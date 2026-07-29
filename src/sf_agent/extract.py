"""Local, layout-preserving text extraction for uploaded documents.

Why this exists: sending a document to a vision-capable provider is the highest-fidelity
way to read it, but also the most expensive, and some providers (Z.AI/GLM) cannot read
`document` or `image` content blocks at all. Most business documents — case studies, account
plans, org charts — are *text* documents whose meaning is fully recoverable locally. This
module recovers it, so those uploads can be structured by the cheap main provider.

The hard part is LAYOUT. Naive text extraction flattens a positional document: a PDF org
chart (names placed in columns to encode seniority) comes out as a bare list of names with
the hierarchy erased — which is worse than useless, because it reads as complete. So each
extractor preserves position:

* PDF   — text runs are collected WITH coordinates, grouped into lines by y, and columns are
          detected by clustering x. A page that looks positional is emitted as CSV (the same
          shape the spreadsheet lane produces, which the ingest guide already knows how to
          read as a grid); a prose page is emitted as plain lines.
* PPTX  — shapes are read natively (never via a PDF render, which loses speaker notes), in
          reading order by position, with tables and notes included.
* DOCX  — paragraphs in order, with tables serialized as Markdown.

Every function returns None when it cannot recover usable text (an image-only/scanned PDF,
an empty deck), which is the caller's signal to fall back to the vision provider.
"""

from __future__ import annotations

import csv
import io
import logging

logger = logging.getLogger("sf_agent.extract")

# Two text runs whose baselines are within this many points belong to the same visual line.
_LINE_TOLERANCE_PT = 3.0
# Two x positions closer than this are the same column. Wider than a word gap, narrower than
# a real column step, so "Jane Doe" stays one cell while separate columns stay separate.
_COLUMN_GAP_PT = 28.0
# A page is treated as a positional GRID (emitted as CSV) rather than prose when it has at
# least this many columns and this share of its lines start past the first column.
_GRID_MIN_COLUMNS = 2
_GRID_MIN_INDENTED_SHARE = 0.25
# ...and only when the page contains no real sentences. A text run this long is prose, not a
# label. Measured on two real documents: a positional org chart had ZERO lines with a run this
# long (max run 11 chars), while a prose case-study page had 44% (max 85) — so any page above
# a small share is prose. This is what stops justified body text (whose wide inter-word gaps
# mimic column boundaries) from being shredded into pseudo-CSV.
_PROSE_RUN_CHARS = 55
_PROSE_MIN_SHARE = 0.10
# Below this many characters a "successful" extraction is treated as failed — the page is
# almost certainly a scan or image-only, so the caller should use vision instead.
_MIN_USEFUL_CHARS = 40


def _cluster(values: list[float], gap: float) -> list[float]:
    """Left edges of clusters formed by splitting sorted `values` wherever the gap exceeds
    `gap`. Used for both column detection and line grouping."""
    out: list[float] = []
    for v in sorted(values):
        if not out or v - out[-1] > gap:
            out.append(v)
    return out


def _rows_to_text(rows: list[list[tuple[float, str]]]) -> str:
    """Render extracted lines, choosing per page between CSV (positional grid) and prose.

    `rows` is a list of lines; each line is a list of (x, text) cells in reading order.

    Telling a positional grid from prose matters and is easy to get wrong: justified body
    text has wide inter-word gaps that look exactly like column boundaries, so gap width
    alone misreads a case study as a spreadsheet. The reliable discriminator is CELL LENGTH
    plus how the lines START — an org chart is short labels beginning at many different
    x offsets, while prose is long sentences all beginning at the same left margin.
    """
    if not rows:
        return ""
    starts = [row[0][0] for row in rows if row]
    columns = _cluster([x for row in rows for x, _ in row], _COLUMN_GAP_PT)

    def col_of(x: float) -> int:
        return max((i for i, c in enumerate(columns) if x >= c - 1), default=0)

    start_columns = {col_of(x) for x in starts}
    indented = sum(1 for x in starts if col_of(x) > 0)
    # Share of lines carrying a sentence-length run. Near zero on a label grid, substantial
    # on any page of real prose.
    with_prose = sum(
        1 for row in rows if row and max((len(t) for _, t in row), default=0) >= _PROSE_RUN_CHARS
    )
    prose_share = with_prose / len(rows)

    is_grid = (
        len(start_columns) >= _GRID_MIN_COLUMNS
        and (indented / len(starts) if starts else 0) >= _GRID_MIN_INDENTED_SHARE
        and prose_share < _PROSE_MIN_SHARE
    )

    if not is_grid:
        # Prose: rebuild each visual line by joining its runs with a single space, so a
        # wrapped sentence reads as a sentence instead of being cut into pseudo-columns.
        return "\n".join(" ".join(t for _, t in row).strip() for row in rows)

    # Grid: emit CSV so a cell's COLUMN INDEX survives — this is what encodes hierarchy in an
    # org chart, and it matches the format the spreadsheet lane already feeds the model.
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    for row in rows:
        cells: list[str] = [""] * len(columns)
        for x, text in row:
            i = col_of(x)
            cells[i] = f"{cells[i]} {text}".strip() if cells[i] else text
        while cells and cells[-1] == "":
            cells.pop()
        writer.writerow(cells)
    return buf.getvalue()


def pdf_text(raw: bytes) -> str | None:
    """Layout-preserving text for a PDF, or None when it has no usable text layer.

    Uses pypdf's visitor hook to capture each text run's position, so positional documents
    (org charts, planning grids) keep their column structure instead of collapsing to a list.
    """
    try:
        from pypdf import PdfReader
    except ImportError:
        logger.info("pypdf not installed; PDF will use the vision path")
        return None

    try:
        reader = PdfReader(io.BytesIO(raw))
    except Exception as e:  # noqa: BLE001 — encrypted/corrupt PDF: let vision try
        logger.info("pypdf could not open the PDF (%s); using the vision path", e)
        return None

    pages_out: list[str] = []
    for page_no, page in enumerate(reader.pages, start=1):
        runs: list[tuple[float, float, str]] = []  # (y, x, text)

        def visitor(text, cm, tm, font_dict, font_size, _runs=runs):  # noqa: ANN001
            t = (text or "").strip()
            if t:
                _runs.append((round(tm[5], 1), round(tm[4], 1), t))

        try:
            page.extract_text(visitor_text=visitor)
        except Exception as e:  # noqa: BLE001 — one bad page shouldn't kill the upload
            logger.info("pypdf failed on page %d (%s); skipping it", page_no, e)
            continue
        if not runs:
            continue

        # Group runs into visual lines by baseline, top of page first.
        baselines = _cluster([y for y, _, _ in runs], _LINE_TOLERANCE_PT)

        def line_of(y: float) -> float:
            return max((b for b in baselines if y >= b - _LINE_TOLERANCE_PT), default=y)

        grouped: dict[float, list[tuple[float, str]]] = {}
        for y, x, t in runs:
            grouped.setdefault(line_of(y), []).append((x, t))
        rows = [
            sorted(cells, key=lambda c: c[0])
            for _, cells in sorted(grouped.items(), key=lambda kv: -kv[0])
        ]
        body = _rows_to_text(rows)
        if body.strip():
            pages_out.append(f"### Page {page_no}\n{body}")

    text = "\n\n".join(pages_out)
    # An image-only/scanned PDF yields little or nothing — signal the caller to use vision.
    return text if len(text.strip()) >= _MIN_USEFUL_CHARS else None


def pptx_text(raw: bytes) -> str | None:
    """Per-slide text for a .pptx/.potx, read natively so speaker notes survive.

    A PDF render of a deck loses the notes pages entirely, so native extraction is strictly
    higher fidelity here as well as cheaper. Shapes are emitted in reading order (top-to-
    bottom, left-to-right) so a deck's positional grids stay coherent.
    """
    try:
        from pptx import Presentation
    except ImportError:
        return None
    try:
        prs = Presentation(io.BytesIO(raw))
    except Exception as e:  # noqa: BLE001
        logger.info("python-pptx could not open the deck (%s); using the vision path", e)
        return None

    out: list[str] = []
    for i, slide in enumerate(prs.slides, start=1):
        rows: list[list[tuple[float, str]]] = []
        tables: list[str] = []
        for shape in slide.shapes:
            top = float(getattr(shape, "top", 0) or 0)
            left = float(getattr(shape, "left", 0) or 0)
            if getattr(shape, "has_table", False):
                # Serialize a real table as Markdown so its grid is unambiguous.
                tbl = shape.table
                lines = []
                for r_i, row in enumerate(tbl.rows):
                    cells = [c.text.strip().replace("\n", " ") for c in row.cells]
                    lines.append("| " + " | ".join(cells) + " |")
                    if r_i == 0:
                        lines.append("|" + "---|" * len(cells))
                tables.append("\n".join(lines))
                continue
            if getattr(shape, "has_text_frame", False):
                for para in shape.text_frame.paragraphs:
                    t = "".join(r.text for r in para.runs).strip() or para.text.strip()
                    if t:
                        # EMU are large; scale to points so the shared thresholds apply.
                        rows.append([(left / 12700.0, t)])
                        top += 1  # keep paragraph order stable within a shape
        body_parts: list[str] = []
        if rows:
            body_parts.append(_rows_to_text(rows))
        body_parts.extend(tables)
        notes = ""
        try:
            if slide.has_notes_slide:
                notes = (slide.notes_slide.notes_text_frame.text or "").strip()
        except Exception:  # noqa: BLE001 — notes are a bonus, never fatal
            notes = ""
        if notes:
            body_parts.append(f"Speaker notes: {notes}")
        body = "\n".join(p for p in body_parts if p.strip())
        if body.strip():
            out.append(f"### Slide {i}\n{body}")

    text = "\n\n".join(out)
    return text if len(text.strip()) >= _MIN_USEFUL_CHARS else None


def docx_text(raw: bytes) -> str | None:
    """Paragraph text for a .docx, with tables serialized as Markdown."""
    try:
        from docx import Document
    except ImportError:
        return None
    try:
        doc = Document(io.BytesIO(raw))
    except Exception as e:  # noqa: BLE001
        logger.info("python-docx could not open the document (%s); using the vision path", e)
        return None

    parts: list[str] = []
    for para in doc.paragraphs:
        t = (para.text or "").strip()
        if t:
            parts.append(t)
    for tbl in doc.tables:
        lines = []
        for r_i, row in enumerate(tbl.rows):
            cells = [c.text.strip().replace("\n", " ") for c in row.cells]
            lines.append("| " + " | ".join(cells) + " |")
            if r_i == 0:
                lines.append("|" + "---|" * len(cells))
        if lines:
            parts.append("\n".join(lines))

    text = "\n".join(parts)
    return text if len(text.strip()) >= _MIN_USEFUL_CHARS else None
