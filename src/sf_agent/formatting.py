"""Deterministic display formatting for answer values.

Presentation used to be the model's job, so it drifted between runs: the same figure came
back as ``13278.7``, ``$13,278.70/hr`` or ``13,279``, and dates as ``2026-07-31`` or
"July 31, 2026". The fix is to have the model emit RAW values and format them here, in code,
where the output cannot vary.

The type of a value is inferred from its FIELD NAME, because nothing upstream carries units:
there is no column-to-unit map anywhere in the warehouse or the prompts, only English
sentences. Inference is deliberately conservative — an unrecognised key formats as a plain
number rather than guessing a currency or percent, since a wrong ``$`` is worse than an
unstyled figure.

Three constraints below come from querying the real data, not from assumption:

* **Never append a rate unit such as "/hr".** ``AGREEDPAYRATE`` mixes hourly (``55.00``) with
  annual (``140000.00``) values, so any suffix would be wrong on real rows. The label carries
  the unit instead.
* **Percentages may be negative or exceed 100.** ``GM`` spans ``-89.50`` to ``118.60`` (mean
  ``11.13``), which also settles a question the codebase never answered: GM is percentage
  points, not a 0-1 ratio and not dollars.
* **Numeric identifiers must never be grouped.** ``ACTIVITYID`` and ``COMPANYID`` are numbers,
  and ``1234567`` must not render as ``1,234,567``.

``static/index.html`` mirrors these rules in JavaScript so the screen, the generated documents
and the spreadsheets agree. Keep the two in step; ``tests/test_formatting.py`` pins the shared
cases.
"""

from __future__ import annotations

import datetime as _dt
import re
from decimal import Decimal
from typing import Any

# --- field-name inference -------------------------------------------------------------
# Matching is TOKEN-based, not substring, because bare substrings misfire badly on real
# column names: 'candidate_name' contains "date", 'corporate_client' contains "rate", and
# 'is_valid' ends in "id". (Same trap as ILIKE '%java%' matching JavaScript.) A key is split
# on separators and camelCase into tokens, and each token is tested whole — with a short
# blocklist for the compound words that still collide.
_SEP_RE = re.compile(r"[^A-Za-z0-9]+")
_CAMEL_RE = re.compile(r"([a-z0-9])([A-Z])")
_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}(?:[ T].*)?$")

# Words that merely CONTAIN a signal word and must not trigger on it.
_FALSE_FRIENDS = {
    "candidate", "candidates", "update", "updated", "mandate", "validate", "consolidate",
    "corporate", "accurate", "separate", "generate", "aggregate", "moderate", "duration",
    "valid", "invalid", "paid", "grid", "hybrid",
}

# Whole-token signals.
_PERCENT_TOKENS = {"pct", "percent", "percentage", "margin", "margins", "gm", "share", "ratio"}
_CURRENCY_TOKENS = {
    "rate", "rates", "revenue", "cost", "costs", "salary", "price", "spend", "billing",
    "amount", "budget", "billrate", "payrate", "agreedbillrate", "agreedpayrate", "runrate",
}
_COUNT_TOKENS = {
    "count", "counts", "headcount", "placements", "total", "totals", "num", "qty",
    "quantity", "rows", "people", "consultants", "clients", "n",
}
_DATE_TOKENS = {
    "date", "dates", "day", "month", "year", "period", "at", "start", "end", "started",
    "ended", "expiry", "expires", "since", "until",
}
_EXPLICIT_CURRENCY_TOKENS = {"usd", "dollar", "dollars", "amt"}


def _tokens(key: Any) -> list[str]:
    """Field name split into lower-case tokens on separators and camelCase boundaries."""
    spaced = _CAMEL_RE.sub(r"\1 \2", str(key or ""))
    return [t for t in _SEP_RE.split(spaced.lower()) if t]


def _hits(tokens: list[str], vocabulary: set[str]) -> bool:
    """True when any token signals membership: an exact match, or a compound token that ends
    with a signal word ('billrate', 'enddate'). False friends are excluded."""
    for token in tokens:
        if token in _FALSE_FRIENDS:
            continue
        if token in vocabulary:
            return True
        if any(len(w) >= 3 and token.endswith(w) for w in vocabulary):
            return True
    return False

CURRENCY = "currency"
PERCENT = "percent"
COUNT = "count"
DATE = "date"
IDENTIFIER = "identifier"
NUMBER = "number"
TEXT = "text"


def classify_field(key: Any) -> str:
    """The display kind implied by a field name. TEXT when nothing matches.

    Order matters where a name carries two signals: 'rate_pct' is a percent (not currency) and
    'margin_usd' is currency (not a percent), so explicit currency is tested before percent,
    and percent before the general currency vocabulary.
    """
    raw = str(key or "").strip()
    if not raw:
        return TEXT
    tokens = _tokens(raw)
    if not tokens:
        return TEXT

    # Identifiers must never be thousands-grouped. A bare 'id' token or an '_id' suffix covers
    # snake_case; a compound ending in "id" covers Snowflake's COMPANYID / ACTIVITYID. The
    # false-friend list is what keeps ordinary words that happen to end in "id" — 'valid',
    # 'paid', 'grid', 'hybrid' — out of this branch.
    if (
        "id" in tokens
        or re.search(r"(^|_)id$", raw, re.I)
        or any(t.endswith("id") and len(t) > 3 and t not in _FALSE_FRIENDS for t in tokens)
    ):
        return IDENTIFIER

    if _hits(tokens, _EXPLICIT_CURRENCY_TOKENS):
        return CURRENCY
    if _hits(tokens, _PERCENT_TOKENS):
        return PERCENT
    if _hits(tokens, _CURRENCY_TOKENS):
        return CURRENCY
    if _hits(tokens, _COUNT_TOKENS):
        return COUNT
    if _hits(tokens, _DATE_TOKENS):
        return DATE
    return TEXT


def _as_number(value: Any) -> float | None:
    """The value as a float when it is genuinely numeric, else None.

    Bools are excluded deliberately: in Python ``True`` is an ``int``, and rendering it as
    ``1`` would lose the Yes/No meaning callers want.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float, Decimal)):
        return float(value)
    return None


def _group(value: float, decimals: int) -> str:
    """Thousands-grouped fixed-decimal string, pinned to en-US so output never depends on
    the viewer's locale (the browser's bare toLocaleString() did)."""
    return f"{value:,.{decimals}f}"


def _trim(text: str) -> str:
    """Drop trailing zeros and any trailing decimal point: 12.50 -> 12.5, 60.00 -> 60."""
    if "." not in text:
        return text
    return text.rstrip("0").rstrip(".")


def format_currency(value: float) -> str:
    """``$1,234.56``; a negative reads ``-$1,234.56`` rather than ``$-1,234.56``.

    No unit suffix is added — see the module docstring on mixed hourly/annual rates.
    """
    sign = "-" if value < 0 else ""
    return f"{sign}${_group(abs(value), 2)}"


def format_percent(value: float) -> str:
    """``12.46%``, keeping negatives and values above 100 intact (GM legitimately has both).

    Values are treated as PERCENTAGE POINTS, not ratios: 12.46 renders 12.46%, not 1246%.
    """
    return f"{_trim(_group(value, 2))}%"


def format_count(value: float) -> str:
    """``1,557``. Whole numbers only; a fractional 'count' keeps one decimal so an average
    dressed as a count is not silently rounded to a lie."""
    if float(value).is_integer():
        return _group(value, 0)
    return _trim(_group(value, 1))


def format_number(value: float) -> str:
    """Default numeric: grouped, up to two decimals, trailing zeros trimmed."""
    if float(value).is_integer():
        return _group(value, 0)
    return _trim(_group(value, 2))


def format_date(value: Any) -> str | None:
    """``Jul 31, 2026`` from a date/datetime or an ISO-ish string; None when unparseable."""
    if isinstance(value, (_dt.date, _dt.datetime)):
        d = value.date() if isinstance(value, _dt.datetime) else value
        return f"{d:%b} {d.day}, {d.year}"
    text = str(value).strip()
    if not _ISO_DATE_RE.match(text):
        return None
    try:
        d = _dt.date.fromisoformat(text[:10])
    except ValueError:
        return None
    return f"{d:%b} {d.day}, {d.year}"


def format_value(value: Any, key: Any = None) -> str | None:
    """Format one value for display, or None when it needs no special handling.

    Returning None (rather than str(value)) lets each caller keep its own convention for
    plain text and for blanks — the UI shows "missing", documents show an em dash.
    """
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return "Yes" if value else "No"

    kind = classify_field(key)

    if kind == IDENTIFIER:
        # Raw, ungrouped. A float id like 1234567.0 still reads as an integer.
        num = _as_number(value)
        if num is not None and num.is_integer():
            return str(int(num))
        return str(value)

    if kind == DATE:
        formatted = format_date(value)
        if formatted:
            return formatted
        # A 'period'/'year' field holding 'Q1FY26' or 2026 is not a date; fall through.

    num = _as_number(value)
    if num is None:
        # Not numeric: a date string under a non-date key still deserves formatting.
        formatted = format_date(value)
        return formatted if formatted else None

    if kind == CURRENCY:
        return format_currency(num)
    if kind == PERCENT:
        return format_percent(num)
    if kind == COUNT:
        return format_count(num)
    return format_number(num)


# Excel keeps values NUMERIC and carries the presentation in a cell number format, so the
# workbook can still sum and chart its own figures. These mirror the string formats above.
_EXCEL_FORMATS = {
    CURRENCY: '"$"#,##0.00',
    PERCENT: '0.00"%"',  # values are percentage points already, so no built-in % (x100) format
    COUNT: "#,##0",
    DATE: "mmm d, yyyy",
    IDENTIFIER: "0",
    NUMBER: "#,##0.##",
}


def excel_number_format(key: Any) -> str | None:
    """The Excel number format for a field, or None to leave the cell as General."""
    return _EXCEL_FORMATS.get(classify_field(key))
