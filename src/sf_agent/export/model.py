"""The format-agnostic document model and the answer -> model mapping.

An ``AgentAnswer`` (as serialized by /api/ask) carries a headline ``value``, a
``summary`` answer, a ``values`` object of supporting figures/lists, and an optional
``chart``. ``answer_to_document`` folds that into a ``DocumentModel`` — a title, a
headline, a summary, a list of ``Table``s, and the chart — that every renderer reads.

The value-shape logic mirrors the web UI's ``renderValue``/``humanize`` so an exported
document lays the data out the same way the on-screen answer does.
"""

from __future__ import annotations

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
    # The agent's normalized chart spec {type, title, labels, series, ...}, or None.
    chart: dict[str, Any] | None = None
    # Data tables the answer drew from, for a provenance footer.
    sources: list[str] = Field(default_factory=list)


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
    scalar_details: list[list[Any]] = []
    values = payload.get("values") or {}
    if isinstance(values, dict):
        for key, v in values.items():
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
        summary=str(payload.get("answer") or ""),
        tables=tables,
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
