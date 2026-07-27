"""Offline tests for the per-answer diagnostics helpers (source, cost, discovery)."""

import json

from sf_agent.agent import (
    _compact_discovery_content,
    _estimate_cost,
    _extract_sources,
    _is_discovery,
)
from sf_agent.types import QueryResult, ToolResult


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
    assert _estimate_cost(usage, "claude-sonnet-4-6") == round(3.0 + 15.0 + 0.30 + 3.75, 6)


def test_estimate_cost_zero_for_empty_usage():
    assert _estimate_cost({}, "claude-sonnet-4-6") == 0.0


def test_estimate_cost_uses_glm_pricing_for_glm_model():
    # The same usage costs less on GLM — the estimate must track the configured provider.
    usage = {"input": 1_000_000, "output": 1_000_000, "cache_read": 0, "cache_write": 0}
    assert _estimate_cost(usage, "glm-5.2") == round(0.60 + 2.20, 6)


# --- _compact_discovery_content ------------------------------------------------


def _peek_result(n_rows: int) -> ToolResult:
    # A realistically wide staffing row: many columns with real string values, which
    # is what a `SELECT *` peek actually returns (and what makes trimming worthwhile).
    cols = [f"col_{i}" for i in range(20)]
    return ToolResult(
        ok=True,
        executed_sql="SELECT * FROM t LIMIT 5",
        result=QueryResult(
            columns=cols,
            rows=[[f"value_{r}_{c}" for c in range(20)] for r in range(n_rows)],
            row_count=n_rows,
            truncated=False,
        ),
    )


def test_compact_discovery_keeps_columns_and_trims_rows():
    payload = json.loads(_compact_discovery_content(_peek_result(5)))
    # all columns preserved (that's what the peek is for) ...
    assert payload["columns"] == [f"col_{i}" for i in range(20)]
    # ... but only two sample rows carried forward, not all five
    assert len(payload["rows"]) == 2
    assert payload["rows"][0] == [f"value_0_{c}" for c in range(20)]
    assert payload["row_count"] == 5
    assert "2 of 5" in payload["note"]


def test_compact_discovery_shorter_than_full_serialization():
    peek = _peek_result(5)
    assert len(_compact_discovery_content(peek)) < len(peek.to_model_text())
