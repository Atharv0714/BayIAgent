"""Render a DocumentModel to a PDF (reportlab / platypus).

Title, headline, summary, then each Table as a real flowable table that splits across
pages with a repeating header. A chartable spec is drawn as a native reportlab graphic
(bar / line / pie) when possible, degrading to its data table otherwise — the numbers
are never lost.
"""

from __future__ import annotations

import io
from typing import Any
from xml.sax.saxutils import escape as _esc

from reportlab.graphics.charts.barcharts import VerticalBarChart
from reportlab.graphics.charts.legends import Legend
from reportlab.graphics.charts.linecharts import HorizontalLineChart
from reportlab.graphics.charts.piecharts import Pie
from reportlab.graphics.shapes import Drawing, String
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import (
    KeepTogether,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table as PdfTable,
    TableStyle,
)

from sf_agent.export.model import DocumentModel, Table, cell_text, chart_series

_ACCENT = colors.HexColor("#7C3AED")
_MUTED = colors.HexColor("#6B6480")
_ROW_ALT = colors.HexColor("#F4F0FB")
# A palette echoing the app's violet/magenta theme, cycled across chart series/slices.
_PALETTE = [colors.HexColor(h) for h in (
    "#7c3aed", "#d6249f", "#a855f7", "#ec4899", "#6366f1",
    "#c026d3", "#8b5cf6", "#f472b6", "#4f46e5", "#db2777",
)]

_MAX_COLS = 8  # wider than this overflows A4; the full grid is always in the xlsx export


def _styles() -> dict[str, ParagraphStyle]:
    base = getSampleStyleSheet()
    return {
        "title": ParagraphStyle("t", parent=base["Title"], textColor=_ACCENT, fontSize=20, spaceAfter=2),
        "subtitle": ParagraphStyle("st", parent=base["Normal"], textColor=_MUTED, fontSize=9, spaceAfter=10),
        "headline": ParagraphStyle("hl", parent=base["Normal"], fontSize=12, spaceAfter=6, textColor=colors.black),
        "body": ParagraphStyle("b", parent=base["Normal"], fontSize=10, leading=14, spaceAfter=10),
        "h2": ParagraphStyle("h2", parent=base["Heading2"], textColor=_ACCENT, fontSize=13, spaceBefore=8, spaceAfter=4),
        "cell": ParagraphStyle("c", parent=base["Normal"], fontSize=8, leading=10),
        "cellhead": ParagraphStyle("ch", parent=base["Normal"], fontSize=8, leading=10, textColor=colors.white, fontName="Helvetica-Bold"),
        "source": ParagraphStyle("src", parent=base["Normal"], fontSize=8, textColor=_MUTED, spaceBefore=10),
    }


def _flow_table(table: Table, st: dict[str, ParagraphStyle], avail_w: float) -> Any:
    cols = table.columns[:_MAX_COLS] or ["Value"]
    truncated = len(table.columns) > _MAX_COLS
    ncols = len(cols)
    header = [Paragraph(str(c), st["cellhead"]) for c in cols]
    data = [header]
    for row in table.rows:
        data.append([
            Paragraph(cell_text(row[i]) if i < len(row) else "", st["cell"])
            for i in range(ncols)
        ])
    col_w = avail_w / ncols
    pdf_tbl = PdfTable(data, colWidths=[col_w] * ncols, repeatRows=1)
    style = [
        ("BACKGROUND", (0, 0), (-1, 0), _ACCENT),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#DDD0F2")),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
    ]
    for r in range(1, len(data)):
        if r % 2 == 0:
            style.append(("BACKGROUND", (0, r), (-1, r), _ROW_ALT))
    pdf_tbl.setStyle(TableStyle(style))
    heading = table.name + ("  (first 8 columns)" if truncated else "")
    return KeepTogether([Paragraph(heading, st["h2"]), pdf_tbl, Spacer(1, 8)])


def _chart_drawing(chart: dict[str, Any]) -> Drawing | None:
    """A native reportlab chart for common types; None when nothing is chartable."""
    labels, series = chart_series(chart)
    if not labels or not series:
        return None
    ctype = str(chart.get("type") or "bar").lower()
    width, height = 460, 240
    d = Drawing(width, height)
    title = str(chart.get("title") or "")
    if title:
        d.add(String(width / 2, height - 12, title, textAnchor="middle", fontSize=11, fillColor=_ACCENT))

    try:
        if ctype in ("pie", "doughnut"):
            name, values = series[0]
            pie = Pie()
            pie.x, pie.y, pie.width, pie.height = 150, 30, 160, 160
            pie.data = values
            pie.labels = [str(l) for l in labels]
            for i in range(len(values)):
                pie.slices[i].fillColor = _PALETTE[i % len(_PALETTE)]
            d.add(pie)
            return d

        if ctype in ("line", "area"):
            chart_obj: Any = HorizontalLineChart()
        else:
            chart_obj = VerticalBarChart()
        chart_obj.x, chart_obj.y = 40, 30
        chart_obj.width, chart_obj.height = width - 80, height - 70
        chart_obj.data = [list(v) for _, v in series]
        chart_obj.categoryAxis.categoryNames = [str(l) for l in labels]
        chart_obj.valueAxis.valueMin = 0
        for i in range(len(series)):
            chart_obj.bars[i].fillColor = _PALETTE[i % len(_PALETTE)] if hasattr(chart_obj, "bars") else None
        if hasattr(chart_obj, "lines"):
            for i in range(len(series)):
                chart_obj.lines[i].strokeColor = _PALETTE[i % len(_PALETTE)]
                chart_obj.lines[i].strokeWidth = 2
        d.add(chart_obj)
        if len(series) > 1:
            legend = Legend()
            legend.x, legend.y = width - 30, height - 30
            legend.alignment = "right"
            legend.fontSize = 8
            legend.colorNamePairs = [
                (_PALETTE[i % len(_PALETTE)], name) for i, (name, _) in enumerate(series)
            ]
            d.add(legend)
        return d
    except Exception:  # noqa: BLE001 — a bad spec falls back to the data table
        return None


def _chart_as_table(chart: dict[str, Any]) -> Table | None:
    labels, series = chart_series(chart)
    if not labels or not series:
        return None
    columns = ["Category"] + [name for name, _ in series]
    rows = [
        [lab] + [(data[i] if i < len(data) else None) for _, data in series]
        for i, lab in enumerate(labels)
    ]
    return Table(name=str(chart.get("title") or "Chart data"), columns=columns, rows=rows)


def render(model: DocumentModel) -> bytes:
    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=A4, title=model.title,
        leftMargin=0.7 * inch, rightMargin=0.7 * inch,
        topMargin=0.7 * inch, bottomMargin=0.7 * inch,
    )
    avail_w = doc.width
    st = _styles()
    story: list[Any] = [
        Paragraph(model.title, st["title"]),
        Paragraph(f"{model.subtitle} · {model.generated_on}", st["subtitle"]),
    ]
    if model.headline:
        story.append(Paragraph(f"<b>Answer:</b> {model.headline}", st["headline"]))
    if model.summary:
        story.append(Paragraph(_esc(model.summary), st["body"]))

    # A deck renders as one headed section per slide (bullets + speaker notes).
    for i, s in enumerate(model.slides, start=1):
        flow: list[Any] = [Paragraph(f"{i}. {_esc(s.title)}", st["h2"])]
        for b in s.bullets:
            flow.append(Paragraph("•&nbsp;&nbsp;" + _esc(b), st["body"]))
        if s.notes:
            flow.append(Paragraph("<i>Notes: " + _esc(s.notes) + "</i>", st["source"]))
        story.append(KeepTogether(flow + [Spacer(1, 6)]))

    if model.chart:
        drawing = _chart_drawing(model.chart)
        if drawing is not None:
            story.append(KeepTogether([drawing, Spacer(1, 10)]))
        else:
            ct = _chart_as_table(model.chart)
            if ct is not None:
                story.append(_flow_table(ct, st, avail_w))

    for table in model.tables:
        story.append(_flow_table(table, st, avail_w))

    if model.sources:
        story.append(Paragraph("Sources: " + ", ".join(model.sources), st["source"]))

    doc.build(story)
    return buf.getvalue()
