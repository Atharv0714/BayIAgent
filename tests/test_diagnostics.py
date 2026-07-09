"""Offline tests for the per-answer diagnostics helpers (source, cost, discovery)."""

from sf_agent.agent import _estimate_cost, _extract_sources, _is_discovery


def test_extract_sources_keeps_real_table_only():
    sqls = [
        "SELECT * FROM placements LIMIT 5",  # shape peek — excluded
        "SELECT * FROM BAYONE_GENERICOVERVIEW LIMIT 5",  # shape peek — excluded
        'SELECT client, COUNT(*) FROM "Billable Data by Job Description" GROUP BY client',
    ]
    assert _extract_sources(sqls) == ["Billable Data by Job Description"]


def test_extract_sources_omits_information_schema_and_dedupes():
    sqls = [
        "SELECT table_name FROM information_schema.tables",
        "SELECT a FROM sales JOIN regions ON sales.r = regions.id",
        "SELECT b FROM sales",  # duplicate source, kept once
    ]
    assert _extract_sources(sqls) == ["sales", "regions"]


def test_is_discovery_only_for_small_select_star():
    assert _is_discovery("SELECT * FROM t LIMIT 5") is True
    assert _is_discovery("select *  from t limit 3") is True
    assert _is_discovery("SELECT * FROM t LIMIT 200") is False  # a real bounded pull
    assert _is_discovery("SELECT col FROM t LIMIT 5") is False  # not SELECT *
    assert _is_discovery("SELECT * FROM t") is False  # no limit -> real query


def test_estimate_cost_uses_per_category_pricing():
    # 1M input @ $3 + 1M output @ $15 + 1M cache_read @ $0.30 + 1M cache_write @ $3.75
    usage = {"input": 1_000_000, "output": 1_000_000, "cache_read": 1_000_000, "cache_write": 1_000_000}
    assert _estimate_cost(usage) == round(3.0 + 15.0 + 0.30 + 3.75, 6)


def test_estimate_cost_zero_for_empty_usage():
    assert _estimate_cost({}) == 0.0
