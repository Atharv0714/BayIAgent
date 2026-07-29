"""The format-agnostic document model and the answer -> model mapping.

An ``AgentAnswer`` (as serialized by /api/ask) carries a headline ``value``, a
``summary`` answer, a ``values`` object of supporting figures/lists, and an optional
``chart``. ``answer_to_document`` folds that into a ``DocumentModel`` — a title, a
headline, a summary, a list of ``Table``s, and the chart — that every renderer reads.

The value-shape logic mirrors the web UI's ``renderValue``/``humanize`` so an exported
document lays the data out the same way the on-screen answer does.
"""

from __future__ import annotations

import re
from datetime import date
from typing import Any

from pydantic import BaseModel, Field

# Abbreviations kept upper-case in humanized labels; mirrors the ACRONYMS set in
# static/index.html so column headings read identically on screen and in exports.
_ACRONYMS = {
    "ID", "SOW", "PO", "PTO", "USA", "US", "UK", "EU", "HPE", "KPI", "SLA", "API",
    "SQL", "URL", "SSN", "DOB", "ZIP", "HR", "IT", "QA", "VP", "CEO", "CTO", "CFO",
    "CIO", "B2B", "B2C", "AI", "ML", "FTE", "JD", "EIN", "W2", "C2C", "PII", "CRM",
    "ERP", "SKU", "YOY", "MOM", "YTD", "MTD", "NDA", "RFP", "RFQ",
}


def humanize(key: Any) -> str:
    """Turn a raw field key ("CANDIDATE_FULL_NAME") into a label ("Candidate Full
    Name"), keeping genuine acronyms upper-case."""
    words = str(key).replace("_", " ").replace("-", " ").split()
    out: list[str] = []
    for w in words:
        up = w.upper()
        if up in _ACRONYMS:
            out.append(up)
        elif len(w) <= 2 and w == up:
            out.append(w)
        else:
            out.append(w[:1].upper() + w[1:].lower())
    return " ".join(out)


class Table(BaseModel):
    """One tabular block: a titled grid of columns and rows. Cells keep their raw
    JSON types (numbers stay numbers) so a renderer can, e.g., right-align or chart
    them; ``cell_text`` handles display coercion."""

    name: str
    columns: list[str]
    rows: list[list[Any]]


class Slide(BaseModel):
    """One slide of a generated deck: a title, bullet lines, and optional speaker notes.

    Produced when the answer carries a ``values.slides`` array (the agent structures a deck
    that way on a "make a presentation" request), so the pptx renderer emits one real slide
    per entry instead of a single table."""

    title: str
    bullets: list[str] = Field(default_factory=list)
    notes: str | None = None


class DocumentModel(BaseModel):
    """Everything a renderer needs to lay out one answer, format-agnostic."""

    title: str
    subtitle: str
    generated_on: str
    # The single headline figure/name the question asked for (AgentAnswer.value).
    headline: str | None = None
    # The plain-language answer summary shown at the top of the on-screen response.
    summary: str = ""
    tables: list[Table] = Field(default_factory=list)
    # A deck, when the answer carried a `values.slides` array — one Slide per entry.
    slides: list[Slide] = Field(default_factory=list)
    # The agent's normalized chart spec {type, title, labels, series, ...}, or None.
    chart: dict[str, Any] | None = None
    # Data tables the answer drew from, for a provenance footer.
    sources: list[str] = Field(default_factory=list)


def strip_markdown(text: Any) -> str:
    """Remove markdown markers so generated documents don't show literal ``**`` and ``-``.

    The answer prompt asks for markdown structure, which the chat UI renders — but the
    PPTX/DOCX/PDF renderers write plain text runs, so the markers would appear verbatim in a
    client-facing deck. Emphasis is dropped rather than converted, because these writers set
    formatting per run and a mid-string bold span cannot be expressed in a single text value.
    """
    s = str(text or "")
    s = re.sub(r"`([^`]*)`", r"\1", s)                       # inline code
    s = re.sub(r"\*\*([^*]+)\*\*", r"\1", s)                  # **bold**
    s = re.sub(r"(?<![\w*])\*([^*\n]+)\*(?![\w*])", r"\1", s)  # *italic*
    s = re.sub(r"(?<![\w_])_([^_\n]+)_(?![\w_])", r"\1", s)    # _italic_
    s = re.sub(r"^\s{0,3}#{1,6}\s+", "", s, flags=re.M)       # ### heading
    s = re.sub(r"^\s{0,3}[-*•]\s+", "", s, flags=re.M)        # - bullet (renderers add their own)
    return s.strip()


def cell_text(v: Any) -> str:
    """Display string for a cell. None/blank becomes an em dash so gaps stay visible
    (never silently dropped); dicts/lists serialize compactly."""
    if v is None or v == "":
        return "—"
    if isinstance(v, bool):
        return "Yes" if v else "No"
    if isinstance(v, (dict, list)):
        import json

        return json.dumps(v, default=str, ensure_ascii=False)
    return str(v)


def _is_obj(x: Any) -> bool:
    return isinstance(x, dict)


def _value_to_table(name: str, v: Any) -> Table | None:
    """Coerce one supporting value into a Table by its shape; None if it's a bare
    scalar (those are collected into a shared Details table instead)."""
    if isinstance(v, list):
        if not v:
            return None
        if all(_is_obj(r) for r in v):
            cols: list[str] = []
            for r in v:
                for k in r:
                    if k not in cols:
                        cols.append(k)
            rows = [[r.get(c) for c in cols] for r in v]
            return Table(name=name, columns=[humanize(c) for c in cols], rows=rows)
        # list of scalars -> single-column table
        return Table(name=name, columns=[name], rows=[[item] for item in v])
    if _is_obj(v):
        if not v:
            return None
        rows = [[humanize(k), iv] for k, iv in v.items()]
        return Table(name=name, columns=["Field", "Value"], rows=rows)
    return None


def _truncate(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _parse_slides(v: Any) -> list[Slide]:
    """Parse a ``values.slides`` array into Slides. Each entry is an object with a
    ``title`` and ``content`` (a list of bullet strings, or a single string that is split
    on newlines); ``notes``/``speaker_notes`` become the slide's speaker notes. Tolerant of
    the field-name variants a model tends to emit."""
    if not isinstance(v, list):
        return []
    slides: list[Slide] = []
    for item in v:
        if isinstance(item, str):
            if item.strip():
                slides.append(Slide(title=item.strip()))
            continue
        if not isinstance(item, dict):
            continue
        title = strip_markdown(item.get("title") or item.get("heading") or item.get("name") or "Slide") or "Slide"
        content = item.get("content")
        if content is None:
            content = item.get("bullets") or item.get("body") or item.get("points")
        bullets: list[str] = []
        if isinstance(content, list):
            bullets = [strip_markdown(cell_text(b)) for b in content if b not in (None, "")]
        elif isinstance(content, str) and content.strip():
            bullets = [strip_markdown(ln) for ln in content.splitlines() if ln.strip()] or [strip_markdown(content)]
        notes = item.get("notes") or item.get("speaker_notes") or item.get("speakerNotes")
        notes = strip_markdown(notes) if notes not in (None, "") else None
        slides.append(Slide(title=title, bullets=bullets, notes=notes))
    return slides


def _is_slides_key(key: str, v: Any) -> bool:
    """True when a `values` entry is a deck: the key is 'slides' and the value is a list of
    slide objects (not, say, a numeric column that happens to be named 'slides')."""
    return key.lower() == "slides" and isinstance(v, list) and any(isinstance(x, dict) for x in v)


def answer_to_document(payload: dict[str, Any], question: str | None = None) -> DocumentModel:
    """Map an /api/ask answer payload into a DocumentModel.

    ``question`` becomes the document title (falling back to a generic one); the
    headline ``value``, ``summary``, ``values`` tables, and ``chart`` carry over. Bare
    scalar values are gathered into a single "Details" key-value table so nothing in
    ``values`` is lost.
    """
    q = (question or "").strip()
    title = _truncate(q, 90) if q else "BayI Intelligence Report"

    value = payload.get("value")
    headline = None
    if value is not None and value != "":
        headline = cell_text(value)

    tables: list[Table] = []
    slides: list[Slide] = []
    scalar_details: list[list[Any]] = []
    values = payload.get("values") or {}
    if isinstance(values, dict):
        for key, v in values.items():
            if _is_slides_key(key, v):
                slides = _parse_slides(v)  # a deck, not a table
                continue
            label = humanize(key)
            table = _value_to_table(label, v)
            if table is not None:
                tables.append(table)
            elif v is not None and v != "":
                scalar_details.append([label, v])
    if scalar_details:
        tables.append(Table(name="Details", columns=["Field", "Value"], rows=scalar_details))

    chart = payload.get("chart")
    if not isinstance(chart, dict):
        chart = None

    sources = payload.get("sources") or []
    if not isinstance(sources, list):
        sources = []

    return DocumentModel(
        title=title,
        subtitle="BayI Intelligence Agent · BayOne",
        generated_on=date.today().isoformat(),
        headline=headline,
        summary=strip_markdown(payload.get("answer")),
        tables=tables,
        slides=slides,
        chart=chart,
        sources=[str(s) for s in sources],
    )


def chart_series(chart: dict[str, Any]) -> tuple[list[str], list[tuple[str, list[float]]]]:
    """Extract (labels, [(series_name, numeric_data), ...]) from a chart spec, dropping
    non-numeric/empty series. Renderers use this to build a native chart; an empty
    series list means there's nothing chartable."""
    labels = [str(x) for x in (chart.get("labels") or [])]
    series_out: list[tuple[str, list[float]]] = []
    for i, s in enumerate(chart.get("series") or []):
        if not isinstance(s, dict):
            continue
        name = str(s.get("name") or f"Series {i + 1}")
        data: list[float] = []
        ok = True
        for d in s.get("data") or []:
            try:
                data.append(float(d))
            except (TypeError, ValueError):
                ok = False
                break
        if ok and data:
            series_out.append((name, data))
    return labels, series_out
