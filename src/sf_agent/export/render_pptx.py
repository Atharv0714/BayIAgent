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
from pptx.enum.shapes import PP_PLACEHOLDER
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
            cell.text = cell_text(row[ci], cols[ci] if ci < len(cols) else None) if ci < len(row) else ""
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
    # A user-uploaded template renders onto ITS theme + layouts (branded, native).
    if template:
        return _render_on_template(model, template)

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


# ── Template mode ──────────────────────────────────────────────────────────────────────
# Render onto the user's uploaded .pptx/.potx so the deck inherits its theme, master, and
# slide layouts. Slides are built by populating the layout's own title/body PLACEHOLDERS
# (so text picks up the template's fonts, colors, and positioning), with a textbox fallback
# on the same branded layout when a placeholder is absent. Tables/charts — rare in a
# "make a deck" answer, which produces bullet slides — are summarized as bullet slides so
# they stay on-template and never overflow an unknown slide size.

_TITLE_TYPES = {PP_PLACEHOLDER.TITLE, PP_PLACEHOLDER.CENTER_TITLE}
_BODY_TYPES = {PP_PLACEHOLDER.BODY, PP_PLACEHOLDER.OBJECT}
_SUBTITLE_TYPES = {PP_PLACEHOLDER.SUBTITLE, PP_PLACEHOLDER.BODY}


def _remove_all_slides(prs: Presentation) -> None:
    """Drop the template's own sample slides, keeping its masters/layouts/theme."""
    lst = prs.slides._sldIdLst
    for sld in list(lst):
        lst.remove(sld)


def _ph_of_type(shapes_holder, types: set) -> Any:
    for ph in shapes_holder.placeholders:
        if ph.placeholder_format.type in types:
            return ph
    return None


def _pick_layouts(prs: Presentation):
    """Choose a title layout (title [+subtitle]) and a content layout (title + body) from
    the template, with sensible fallbacks so any template yields something usable."""
    layouts = list(prs.slide_layouts)
    has_title = lambda l: _ph_of_type(l, _TITLE_TYPES) is not None  # noqa: E731
    has_body = lambda l: _ph_of_type(l, _BODY_TYPES) is not None  # noqa: E731
    has_sub = lambda l: _ph_of_type(l, {PP_PLACEHOLDER.SUBTITLE}) is not None  # noqa: E731
    title_layout = (
        next((l for l in layouts if has_title(l) and has_sub(l)), None)
        or next((l for l in layouts if has_title(l)), None)
        or layouts[0]
    )
    content_layout = (
        next((l for l in layouts if has_title(l) and has_body(l)), None)
        or next((l for l in layouts if has_body(l)), None)
        or title_layout
    )
    return title_layout, content_layout


def _fallback_textbox(prs: Presentation, slide, text: str, top) -> None:
    box = slide.shapes.add_textbox(Inches(0.7), top, prs.slide_width - Inches(1.4), Inches(1.2))
    tf = box.text_frame
    tf.word_wrap = True
    tf.text = text


def _tpl_title_slide(prs: Presentation, layout, model: DocumentModel) -> None:
    slide = prs.slides.add_slide(layout)
    if slide.shapes.title is not None:
        slide.shapes.title.text = model.title
    subtitle = f"{model.subtitle} · {model.generated_on}"
    if model.headline:
        subtitle += f"\nAnswer: {model.headline}"
    sub = _ph_of_type(slide, _SUBTITLE_TYPES)
    if sub is not None:
        sub.text_frame.text = subtitle
    else:
        _fallback_textbox(prs, slide, subtitle, top=Inches(4.2))


def _tpl_content_slide(prs: Presentation, layout, title: str, bullets: list[str], notes: str | None = None) -> None:
    slide = prs.slides.add_slide(layout)
    if slide.shapes.title is not None:
        slide.shapes.title.text = title
    else:
        _fallback_textbox(prs, slide, title, top=Inches(0.4))
    body = _ph_of_type(slide, _BODY_TYPES)
    if bullets and body is not None:
        tf = body.text_frame
        tf.clear()
        for i, b in enumerate(bullets):
            p = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
            p.text = b
    elif bullets:
        _fallback_textbox(prs, slide, "\n".join("•  " + b for b in bullets), top=Inches(1.6))
    if notes:
        slide.notes_slide.notes_text_frame.text = notes


def _table_as_bullets(table: Table) -> list[str]:
    """One bullet per row (col: value · col: value), capped — keeps it on-template."""
    out: list[str] = []
    for row in table.rows[:_MAX_ROWS]:
        pairs = [f"{table.columns[i]}: {cell_text(v, table.columns[i])}" for i, v in enumerate(row) if i < len(table.columns)]
        out.append(" · ".join(pairs))
    return out


def _chart_as_bullets(chart: dict[str, Any]) -> list[str]:
    labels, series = chart_series(chart)
    if not labels or not series:
        return []
    _name, data = series[0]
    return [f"{lab}: {data[i] if i < len(data) else ''}" for i, lab in enumerate(labels)]


def _render_on_template(model: DocumentModel, template: bytes) -> bytes:
    prs = Presentation(io.BytesIO(template))  # inherits the template's theme + layouts
    _remove_all_slides(prs)
    title_layout, content_layout = _pick_layouts(prs)

    _tpl_title_slide(prs, title_layout, model)
    if model.summary and not model.slides:
        # Only add a stand-alone summary slide when there isn't already a deck of slides.
        _tpl_content_slide(prs, content_layout, "Summary", [model.summary])
    for s in model.slides:
        _tpl_content_slide(prs, content_layout, s.title, s.bullets, notes=s.notes)
    for table in model.tables:
        _tpl_content_slide(prs, content_layout, table.name, _table_as_bullets(table))
    if model.chart:
        bullets = _chart_as_bullets(model.chart)
        if bullets:
            _tpl_content_slide(prs, content_layout, str(model.chart.get("title") or "Chart"), bullets)

    buf = io.BytesIO()
    prs.save(buf)
    return buf.getvalue()
