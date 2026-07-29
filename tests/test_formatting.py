"""Display formatting rules — the shared contract between the UI, documents and Excel.

These pin the traps found by querying the real warehouse, not hypotheticals:
  * AGREEDPAYRATE mixes hourly (55.00) and annual (140000.00), so currency must NOT gain a
    "/hr" suffix;
  * GM spans -89.50 to 118.60, so percents must keep negatives and values over 100;
  * ACTIVITYID / COMPANYID are numeric, so identifiers must never be thousands-grouped.
The JavaScript in static/index.html mirrors this module; when a rule changes here, change it
there too.
"""

import datetime as dt
from decimal import Decimal

import pytest

from sf_agent.formatting import (
    COUNT,
    CURRENCY,
    DATE,
    IDENTIFIER,
    PERCENT,
    TEXT,
    classify_field,
    excel_number_format,
    format_value,
)


@pytest.mark.parametrize(
    "key,expected",
    [
        ("agreedbillrate", CURRENCY),
        ("bill_rate", CURRENCY),
        ("total_revenue", CURRENCY),
        ("run_rate_usd", CURRENCY),
        ("margin_usd", CURRENCY),      # explicit currency beats the 'margin' percent signal
        ("gm", PERCENT),
        ("gross_margin", PERCENT),
        ("rate_pct", PERCENT),         # 'pct' beats the 'rate' currency signal
        ("utilization_percent", PERCENT),
        ("headcount", COUNT),
        ("placement_count", COUNT),
        ("num_clients", COUNT),
        ("end_date", DATE),
        ("start_date", DATE),
        ("extracted_at", DATE),
        ("companyid", IDENTIFIER),
        ("client_id", IDENTIFIER),
        ("candidate_name", TEXT),
        ("primary_skill", TEXT),
    ],
)
def test_field_classification(key, expected):
    assert classify_field(key) == expected


def test_currency_never_gains_a_rate_suffix():
    """AGREEDPAYRATE holds hourly AND annual values, so a '/hr' suffix would be wrong."""
    assert format_value(55.0, "agreedpayrate") == "$55.00"
    assert format_value(140000.0, "agreedpayrate") == "$140,000.00"
    for out in (format_value(55.0, "agreedpayrate"), format_value(140000.0, "agreedpayrate")):
        assert "/hr" not in out and "hour" not in out.lower()


def test_currency_groups_and_signs():
    assert format_value(13278.7, "run_rate") == "$13,278.70"
    assert format_value(Decimal("105.00"), "agreedbillrate") == "$105.00"
    # Sign leads the symbol, so it reads as a negative amount rather than "$-1,000.00".
    assert format_value(-1000, "revenue") == "-$1,000.00"


def test_percent_keeps_negatives_and_over_one_hundred():
    """GM legitimately ranges -89.50 to 118.60 in this warehouse."""
    assert format_value(12.46, "gm") == "12.46%"
    assert format_value(-89.5, "gm") == "-89.5%"
    assert format_value(118.6, "gm") == "118.6%"
    # Percentage POINTS, not a ratio: 12.46 must not become 1246%.
    assert format_value(60, "margin_pct") == "60%"


def test_identifiers_are_never_grouped():
    assert format_value(1234567, "companyid") == "1234567"
    assert format_value(1234567.0, "activityid") == "1234567"
    assert "," not in format_value(9876543, "client_id")


def test_counts_group_but_keep_a_fractional_average_honest():
    assert format_value(1557, "placement_count") == "1,557"
    # An average hiding under a 'count' name must not round to a different number.
    assert format_value(12.5, "avg_count") == "12.5"


def test_dates_render_consistently():
    assert format_value("2026-07-31", "end_date") == "Jul 31, 2026"
    assert format_value(dt.date(2026, 7, 31), "end_date") == "Jul 31, 2026"
    assert format_value("2026-07-31T00:00:00", "extracted_at") == "Jul 31, 2026"
    # A date string under a non-date key is still formatted rather than dumped raw.
    assert format_value("2026-07-31", "when") == "Jul 31, 2026"


def test_non_date_values_under_date_like_keys_fall_through():
    """'period' and 'year' fields hold things like 'Q1FY26' — not parseable dates."""
    assert format_value("Q1FY26", "period") is None
    assert format_value(2026, "year") == "2,026" or format_value(2026, "year") == "2026"


def test_blanks_and_booleans():
    assert format_value(None, "revenue") is None
    assert format_value("", "revenue") is None
    assert format_value(True, "is_active") == "Yes"
    assert format_value(False, "is_active") == "No"


def test_plain_text_is_left_alone():
    assert format_value("Google", "client_name") is None
    assert format_value("UX Researcher", "job_title") is None


def test_unknown_numeric_key_gets_a_plain_grouped_number_not_a_guess():
    """A wrong '$' or '%' is worse than an unstyled figure."""
    out = format_value(13278.7, "some_unlabelled_metric")
    assert out == "13,278.7"
    assert "$" not in out and "%" not in out


def test_excel_formats_keep_values_numeric():
    assert excel_number_format("agreedbillrate") == '"$"#,##0.00'
    assert excel_number_format("gm") == '0.00"%"'
    assert excel_number_format("headcount") == "#,##0"
    assert excel_number_format("end_date") == "mmm d, yyyy"
    assert excel_number_format("companyid") == "0"
    assert excel_number_format("candidate_name") is None


@pytest.mark.parametrize(
    "key,expected",
    [
        # Substring traps: these words merely CONTAIN a signal word.
        ("candidate_name", TEXT),      # contains "date"
        ("CANDIDATENAME", TEXT),
        ("corporate_client", TEXT),    # contains "rate"
        ("is_valid", TEXT),            # ends in "id"
        ("duration_weeks", TEXT),      # contains "rat"/"at"
        # ...while the real signals still land.
        ("AGREEDBILLRATE", CURRENCY),
        ("END_DATE", DATE),
        ("enddate", DATE),
        ("COMPANYID", IDENTIFIER),
        ("ACTIVITYID", IDENTIFIER),
        ("billRate", CURRENCY),        # camelCase split
        ("totalPlacements", COUNT),
    ],
)
def test_token_matching_avoids_substring_traps(key, expected):
    assert classify_field(key) == expected
