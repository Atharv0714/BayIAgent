"""Render a DocumentModel to a native .pptx deck (python-pptx).

A title slide, a summary slide (headline + answer), one slide per Table (a real
PowerPoint table, capped to what fits), and — for a chartable spec — a slide with a
native, editable PowerPoint chart. Built for a rep to drop straight into a client deck.
"""

from __future__ import annotations

import io
from typing import Any

from pptx import Presentation
from pptx.chart.data import CategoryChartData
from pptx.dml.color import RGBColor
from pptx.enum.chart import XL_CHART_TYPE
from pptx.enum.text import PP_ALIGN
from pptx.util import Emu, Inches, Pt

from sf_agent.export.model import DocumentModel, Table, cell_text, chart_series

_ACCENT = RGBColor(0x7C, 0x3A, 0xED)
_MUTED = RGBColor(0x6B, 0x64, 0x80)
_WHITE = RGBColor(0xFF, 0xFF, 0xFF)

# A content slide can't hold an unbounded grid; cap to what stays legible and note when
# the source had more (the full data always lives in the xlsx export).
_MAX_ROWS = 12
_MAX_COLS = 6

_SLIDE_W = Inches(13.333)
_SLIDE_H = Inches(7.5)


def _title_slide(prs: Presentation, model: DocumentModel) -> None:
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    box = slide.shapes.add_textbox(Inches(0.8), Inches(2.4), Inches(11.7), Inches(2.5))
    tf = box.text_frame
    tf.word_wrap = True
    p = tf.paragraphs[0]
    r = p.add_run()
    r.text = model.title
    r.font.size = Pt(34)
    r.font.bold = True
    r.font.color.rgb = _ACCENT
    p2 = tf.add_paragraph()
    r2 = p2.add_run()
    r2.text = f"{model.subtitle} · {model.generated_on}"
    r2.font.size = Pt(14)
    r2.font.color.rgb = _MUTED
    if model.headline:
        p3 = tf.add_paragraph()
        r3 = p3.add_run()
        r3.text = f"Answer: {model.headline}"
        r3.font.size = Pt(18)
        r3.font.bold = True


def _heading(slide, text: str) -> None:
    box = slide.shapes.add_textbox(Inches(0.6), Inches(0.35), Inches(12.1), Inches(0.8))
    tf = box.text_frame
    tf.word_wrap = True
    p = tf.paragraphs[0]
    r = p.add_run()
    r.text = text
    r.font.size = Pt(24)
    r.font.bold = True
    r.font.color.rgb = _ACCENT


def _summary_slide(prs: Presentation, model: DocumentModel) -> None:
    if not model.summary:
        return
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    _heading(slide, "Summary")
    box = slide.shapes.add_textbox(Inches(0.7), Inches(1.5), Inches(11.9), Inches(5.2))
    tf = box.text_frame
    tf.word_wrap = True
    tf.paragraphs[0].text = model.summary
    for para in tf.paragraphs:
        for run in para.runs:
            run.font.size = Pt(16)


def _table_slide(prs: Presentation, table: Table) -> None:
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    truncated_cols = len(table.columns) > _MAX_COLS
    truncated_rows = len(table.rows) > _MAX_ROWS
    cols = table.columns[:_MAX_COLS] or ["Value"]
    rows = table.rows[:_MAX_ROWS]

    note = ""
    if truncated_rows or truncated_cols:
        note = f"  (showing {len(rows)} of {len(table.rows)} rows)"
    _heading(slide, table.name + note)

    ncols = len(cols)
    nrows = len(rows) + 1
    left, top, width = Inches(0.6), Inches(1.4), Inches(12.1)
    height = Inches(min(5.6, 0.4 * nrows))
    shape = slide.shapes.add_table(nrows, ncols, left, top, width, height)
    tbl = shape.table
    for i, col in enumerate(cols):
        cell = tbl.cell(0, i)
        cell.text = str(col)
        cell.fill.solid()
        cell.fill.fore_color.rgb = _ACCENT
        para = cell.text_frame.paragraphs[0]
        para.font.bold = True
        para.font.size = Pt(11)
        para.font.color.rgb = _WHITE
    for ri, row in enumerate(rows, start=1):
        for ci in range(ncols):
            cell = tbl.cell(ri, ci)
            cell.text = cell_text(row[ci]) if ci < len(row) else ""
            cell.text_frame.paragraphs[0].font.size = Pt(10)


def _chart_slide(prs: Presentation, chart: dict[str, Any]) -> None:
    labels, series = chart_series(chart)
    if not labels or not series:
        return
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    _heading(slide, str(chart.get("title") or "Visualization"))

    ctype = str(chart.get("type") or "bar").lower()
    if ctype in ("line", "area"):
        xl_type = XL_CHART_TYPE.LINE_MARKERS
    elif ctype in ("pie", "doughnut"):
        xl_type = XL_CHART_TYPE.PIE
        series = series[:1]  # a pie shows a single series
    else:
        xl_type = XL_CHART_TYPE.COLUMN_CLUSTERED

    data = CategoryChartData()
    data.categories = labels
    for name, values in series:
        # Pad/trim each series to the category count so python-pptx doesn't misalign.
        padded = [(values[i] if i < len(values) else None) for i in range(len(labels))]
        data.add_series(name, padded)

    slide.shapes.add_chart(
        xl_type, Inches(0.7), Inches(1.4), Inches(11.9), Inches(5.5), data
    )


def _content_slide(prs: Presentation, s) -> None:
    """One deck slide: title + bullet body, with any speaker notes on the notes page."""
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    _heading(slide, s.title)
    if s.bullets:
        box = slide.shapes.add_textbox(Inches(0.8), Inches(1.5), Inches(11.7), Inches(5.2))
        tf = box.text_frame
        tf.word_wrap = True
        for i, b in enumerate(s.bullets):
            p = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
            p.text = "•  " + b
            p.font.size = Pt(18)
            p.space_after = Pt(8)
    if s.notes:
        slide.notes_slide.notes_text_frame.text = s.notes


def render(model: DocumentModel, template: bytes | None = None) -> bytes:
    # `template` (a user-uploaded .pptx/.potx) will render the deck onto that template's
    # theme + layouts — implemented in the next chunk. For now it falls back to the
    # built-in default deck so the plumbing is in place and callers work.
    prs = Presentation()
    prs.slide_width = Emu(int(_SLIDE_W))
    prs.slide_height = Emu(int(_SLIDE_H))

    _title_slide(prs, model)
    _summary_slide(prs, model)
    # A deck: one real slide per entry (title + bullets + speaker notes).
    for s in model.slides:
        _content_slide(prs, s)
    if model.chart:
        _chart_slide(prs, model.chart)
    for table in model.tables:
        _table_slide(prs, table)

    buf = io.BytesIO()
    prs.save(buf)
    return buf.getvalue()
