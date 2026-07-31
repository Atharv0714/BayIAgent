"""Render a DocumentModel to a native .pptx deck (python-pptx).

A title slide, a summary slide (headline + answer), one slide per Table (a real
PowerPoint table, capped to what fits), and — for a chartable spec — a slide with a
native, editable PowerPoint chart. Built for a rep to drop straight into a client deck.
"""

from __future__ import annotations

import io
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from lxml import etree
from pptx import Presentation
from pptx.chart.data import CategoryChartData
from pptx.dml.color import RGBColor
from pptx.enum.chart import XL_CHART_TYPE
from pptx.enum.shapes import PP_PLACEHOLDER
from pptx.enum.text import PP_ALIGN
from pptx.oxml.ns import qn
from pptx.util import Emu, Inches, Pt

from sf_agent.export.model import DocumentModel, Table, cell_text, chart_series

_ACCENT = RGBColor(0x7C, 0x3A, 0xED)
_MUTED = RGBColor(0x6B, 0x64, 0x80)
_WHITE = RGBColor(0xFF, 0xFF, 0xFF)

# A content slide can't hold an unbounded grid; cap to what stays legible and note when
# the source had more (the full data always lives in the xlsx export).
_MAX_ROWS = 12
_MAX_COLS = 6

# Legibility floors for type read off a template. A designed deck packs its slides, so its
# dominant sizes can be small; a generated deck carries a few bullets per slide, where the
# same size reads as tiny. Family, weight and colour are always the template's.
_MIN_TITLE_PT = 20.0
_MIN_BODY_PT = 14.0

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


def render(
    model: DocumentModel, template: bytes | None = None, template_mode: str = "theme"
) -> bytes:
    # A user-uploaded template renders onto ITS theme + layouts (branded, native).
    # template_mode="theme_and_headers" additionally keeps the template's own slide titles.
    if template:
        return _render_on_template(
            model, template, keep_headers=(template_mode == "theme_and_headers")
        )

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
    """Drop the template's own sample slides, keeping its masters/layouts/theme.

    Removing the ``<p:sldId>`` entries alone is NOT enough, and the difference is what makes
    PowerPoint offer to "repair" the generated deck. The presentation part keeps its
    relationship to each old slide, so those slide parts — and the notes pages and images
    they own — stay reachable and get written to the .pptx. Meanwhile python-pptx allocates
    the next slide partname as ``slide{len(sldIdLst) + 1}.xml``, and sldIdLst is now empty, so
    the new slides are handed the SAME partnames. The saved package then contains two
    ``ppt/slides/slide1.xml`` entries, plus orphan notesSlides claiming slides that point at a
    different notes page — a slide with two notes pages is exactly what PowerPoint rejects.

    So drop each slide's own relationships first (its notes page, its pictures), then the
    presentation's relationship to the slide, and only then the sldIdLst entry. Parts still
    reachable elsewhere — layouts via the master, a logo shared with a layout — survive, since
    the package is serialized by walking relationships.
    """
    sldIdLst = prs.slides._sldIdLst
    pres_part = prs.part
    for sldId in list(sldIdLst):
        rId = sldId.rId
        slide_part = pres_part.related_part(rId)
        for rel_id, rel in list(slide_part.rels.items()):
            if not rel.is_external:
                slide_part.rels.pop(rel_id)
        sldIdLst.remove(sldId)
        pres_part.rels.pop(rId)


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


# ── Templates whose design lives in their SLIDES, not their theme ──────────────────────
# A corporate .potx puts its identity in the master, the layouts and the theme, so
# inheriting those (and dropping the sample slides) reproduces the look. Plenty of real
# decks are not built that way: anything exported from Canva, Figma or Google Slides — and
# anything generated by a script — ships the stock Office theme and ONE empty layout, and
# draws every slide's background, colours and type directly on the slide.
#
# For those, keeping the theme and discarding the slides keeps nothing: the result is white
# slides in default Calibri, which reads as "it gave me a blank deck". So before the sample
# slides are dropped we read the look off them — background, title type, body type — and
# apply it to the slides we generate.


@dataclass(frozen=True)
class _TextStyle:
    """Font of one kind of text in the template (its titles, or its body copy)."""

    name: str | None = None
    size: Any = None  # Pt()
    bold: bool | None = None
    color: RGBColor | None = None

    def apply(self, run) -> None:
        if self.name:
            run.font.name = self.name
        if self.size is not None:
            run.font.size = self.size
        if self.bold is not None:
            run.font.bold = self.bold
        if self.color is not None:
            run.font.color.rgb = self.color


@dataclass(frozen=True)
class _TemplateLook:
    """A template's visual identity, read off its own slides."""

    background: bytes | None = None  # a <p:bg> element, serialized
    title: _TextStyle = _TextStyle()
    body: _TextStyle = _TextStyle()


def _run_style(run) -> tuple[float | None, str | None, bool | None, str | None]:
    """(size in points, font name, bold, RRGGBB) for one run, with unset parts as None."""
    size = run.font.size.pt if run.font.size is not None else None
    color = None
    try:  # a theme-coloured run raises rather than reporting an RGB value
        c = run.font.color
        if c is not None and c.type is not None and c.rgb is not None:
            color = str(c.rgb)
    except (AttributeError, TypeError, ValueError):
        color = None
    return size, run.font.name, run.font.bold, color


def _slide_runs(slide) -> list[Any]:
    return [
        r
        for sh in slide.shapes
        if getattr(sh, "has_text_frame", False)
        for para in sh.text_frame.paragraphs
        for r in para.runs
        if r.text.strip()
    ]


def _weighted(weights: Counter, index: int) -> Any:
    """The heaviest non-None value of one style attribute across weighted style tuples."""
    totals: Counter = Counter()
    for style, weight in weights.items():
        if style[index] is not None:
            totals[style[index]] += weight
    return totals.most_common(1)[0][0] if totals else None


def _as_rgb(hex_color: str | None) -> RGBColor | None:
    try:
        return RGBColor.from_string(hex_color) if hex_color else None
    except (ValueError, TypeError):
        return None


def _design_is_on_the_slides(prs: Presentation) -> bool:
    """Does this template carry its identity on its slides rather than in its layouts?

    Asking whether SOME layout has placeholders is not the question — a deck exported from
    a design tool, or generated by python-pptx, ships the stock Office layouts untouched
    alongside slides that use none of them. What matters is what the template's own slides
    do: if not one of them fills a placeholder, its look is drawn on the slides, and
    inheriting the layouts inherits nothing.
    """
    slides = list(prs.slides)
    if not slides:
        return False  # a real .potx has no sample slides; its layouts are the design
    return not any(shape.is_placeholder for slide in slides for shape in slide.shapes)


def _most_used_layout(prs: Presentation):
    """The layout the template's own slides sit on — the canvas its design was drawn on."""
    counts: Counter = Counter()
    by_partname = {}
    for slide in prs.slides:
        layout = slide.slide_layout
        key = str(layout.part.partname)  # SlideLayout itself is not hashable
        counts[key] += 1
        by_partname[key] = layout
    if not counts:
        return prs.slide_layouts[0]
    return by_partname[counts.most_common(1)[0][0]]


def _extract_look(prs: Presentation) -> _TemplateLook:
    """Read a template's background and typography off its sample slides.

    Titles are taken from the largest run on each slide; body copy from the remaining runs,
    weighted by how much text is set in each style, so the dominant body font wins over an
    incidental label. Sizes are the most common rather than the largest, so one oversized
    cover slide doesn't set the tone for the whole deck.
    """
    bg_counts: Counter[bytes] = Counter()
    # style tuple -> characters set in it. Weighting by how much text carries a style, not
    # how many runs use it, is what separates real body copy from the many short bold
    # labels a designed slide is covered in — counting runs picked a 10.5pt bold caption
    # as "body" on a deck whose prose is 12.5pt regular.
    title_weight: Counter[tuple] = Counter()
    body_weight: Counter[tuple] = Counter()

    for slide in prs.slides:
        for bg in slide._element.xpath("./p:cSld/p:bg"):
            xml = etree.tostring(bg)
            # A picture background carries a relationship to a media part that would not
            # survive being cloned onto a different slide; solid and gradient fills are
            # self-contained, so only those are reused.
            if b"r:embed" not in xml and b"r:link" not in xml:
                bg_counts[xml] += 1

        styles = [(r, _run_style(r)) for r in _slide_runs(slide)]
        sizes = [s[0] for _, s in styles if s[0] is not None]
        top_size = max(sizes) if sizes else None
        for run, style in styles:
            bucket = title_weight if (top_size is not None and style[0] == top_size) else body_weight
            bucket[style] += len(run.text)

    def style_of(weights: Counter, min_pt: float) -> _TextStyle:
        """The single style most of this text is actually set in.

        Voting attribute-by-attribute yields combinations that appear nowhere in the deck
        (body copy came out bold because bold labels won that one field), so take the most
        common WHOLE style. A missing font or colour is filled from the wider sample, since
        leaving those unset means falling back to black Calibri — the blank look this is
        here to avoid. ``bold`` is NOT filled in: absent means "not bold", a real answer.

        Size is floored. A designed template packs its slides, so its dominant body size can
        be small (10.5pt here); the decks generated from it carry a handful of bullets, where
        that reads as tiny. The family, weight and colour still come from the template, so
        the deck keeps its identity.
        """
        if not weights:
            return _TextStyle()
        size, name, bold, color = weights.most_common(1)[0][0]
        size = size if size is not None else _weighted(weights, 0)
        name = name or _weighted(weights, 1)
        color = color or _weighted(weights, 3)
        return _TextStyle(
            name=name,
            size=Pt(max(size, min_pt)) if size else Pt(min_pt),
            bold=bold,
            color=_as_rgb(color),
        )

    return _TemplateLook(
        background=bg_counts.most_common(1)[0][0] if bg_counts else None,
        title=style_of(title_weight, _MIN_TITLE_PT),
        body=style_of(body_weight, _MIN_BODY_PT),
    )


def _apply_background(slide, background: bytes | None) -> None:
    """Put the template's background on a generated slide (<p:bg> leads <p:cSld>)."""
    if not background:
        return
    cSld = slide._element.find(qn("p:cSld"))
    if cSld is None:
        return
    for existing in cSld.findall(qn("p:bg")):
        cSld.remove(existing)
    cSld.insert(0, etree.fromstring(background))


def _fallback_textbox(
    prs: Presentation, slide, text: str, top, style: _TextStyle | None = None, height=Inches(1.2)
) -> None:
    """A plain textbox, used when the template's layout offers no placeholder to fill.

    ``style`` carries the template's own type so this path inherits its look instead of
    falling back to black Calibri on white.
    """
    box = slide.shapes.add_textbox(Inches(0.7), top, prs.slide_width - Inches(1.4), height)
    tf = box.text_frame
    tf.word_wrap = True
    for i, line in enumerate(text.split("\n")):
        p = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
        run = p.add_run()
        run.text = line
        if style is not None:
            style.apply(run)


def _tpl_title_slide(
    prs: Presentation, layout, model: DocumentModel, look: _TemplateLook | None = None
) -> None:
    slide = prs.slides.add_slide(layout)
    _apply_background(slide, look.background if look else None)
    subtitle = f"{model.subtitle} · {model.generated_on}"
    if model.headline:
        subtitle += f"\nAnswer: {model.headline}"
    if slide.shapes.title is not None:
        slide.shapes.title.text = model.title
    else:
        _fallback_textbox(
            prs, slide, model.title, top=Inches(2.6),
            style=look.title if look else None, height=Inches(1.6),
        )
    sub = _ph_of_type(slide, _SUBTITLE_TYPES)
    if sub is not None:
        sub.text_frame.text = subtitle
    else:
        _fallback_textbox(prs, slide, subtitle, top=Inches(4.2), style=look.body if look else None)


def _tpl_content_slide(
    prs: Presentation,
    layout,
    title: str,
    bullets: list[str],
    notes: str | None = None,
    look: _TemplateLook | None = None,
) -> None:
    slide = prs.slides.add_slide(layout)
    _apply_background(slide, look.background if look else None)
    if slide.shapes.title is not None:
        slide.shapes.title.text = title
    else:
        _fallback_textbox(prs, slide, title, top=Inches(0.5), style=look.title if look else None)
    body = _ph_of_type(slide, _BODY_TYPES)
    if bullets and body is not None:
        tf = body.text_frame
        tf.clear()
        for i, b in enumerate(bullets):
            p = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
            p.text = b
    elif bullets:
        _fallback_textbox(
            prs, slide, "\n".join("•  " + b for b in bullets), top=Inches(1.9),
            style=look.body if look else None, height=Inches(4.6),
        )
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


def _template_headers(prs: Presentation) -> list[str]:
    """The template's own slide titles, in order, before its sample slides are dropped.

    Used for template_mode="theme_and_headers", where the user asked to keep the uploaded
    deck's section headings and not merely its look."""
    headers: list[str] = []
    for slide in prs.slides:
        title = None
        try:
            if slide.shapes.title is not None:
                title = (slide.shapes.title.text or "").strip()
        except (AttributeError, ValueError):  # a layout without a title placeholder
            title = None
        if not title:
            # Fall back to the topmost non-empty text box, which is the visual header.
            texts = [
                (float(getattr(sh, "top", 0) or 0), sh.text_frame.text.strip())
                for sh in slide.shapes
                if getattr(sh, "has_text_frame", False) and sh.text_frame.text.strip()
            ]
            if texts:
                title = min(texts, key=lambda t: t[0])[1].splitlines()[0].strip()
        if title:
            headers.append(title[:120])
    return headers


def _render_on_template(
    model: DocumentModel, template: bytes, keep_headers: bool = False
) -> bytes:
    prs = Presentation(io.BytesIO(template))  # inherits the template's theme + layouts
    headers = _template_headers(prs) if keep_headers else []
    if _design_is_on_the_slides(prs):
        # Nothing to inherit from the layouts: read the background and type off the sample
        # slides before they are dropped, and build on the canvas they used. Without this
        # the deck comes back white with default Calibri — "it gave me a blank deck".
        look = _extract_look(prs)
        title_layout = content_layout = _most_used_layout(prs)
    else:
        look = None
        title_layout, content_layout = _pick_layouts(prs)
    _remove_all_slides(prs)

    _tpl_title_slide(prs, title_layout, model, look=look)
    if model.summary and not model.slides:
        # Only add a stand-alone summary slide when there isn't already a deck of slides.
        _tpl_content_slide(prs, content_layout, "Summary", [model.summary], look=look)
    for i, s in enumerate(model.slides):
        # theme_and_headers: the uploaded deck's own heading for this position wins, so the
        # new deck follows the template's section structure with our content underneath.
        heading = headers[i] if (keep_headers and i < len(headers)) else s.title
        _tpl_content_slide(prs, content_layout, heading, s.bullets, notes=s.notes, look=look)
    for table in model.tables:
        _tpl_content_slide(prs, content_layout, table.name, _table_as_bullets(table), look=look)
    if model.chart:
        bullets = _chart_as_bullets(model.chart)
        if bullets:
            _tpl_content_slide(
                prs, content_layout, str(model.chart.get("title") or "Chart"), bullets, look=look
            )

    buf = io.BytesIO()
    prs.save(buf)
    return buf.getvalue()
