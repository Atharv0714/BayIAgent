"""Offline tests for the query router + the web/usage helpers on the agent.

These exercise pure logic (JSON parsing, usage folding, citation extraction) with no
network, so they run without Snowflake or Anthropic creds.
"""

from types import SimpleNamespace

from sf_agent.agent import _extract_web, _merge_usage
from sf_agent.router import (
    ROUTE_DATABASE,
    ROUTE_FOLLOWUP,
    ROUTE_WEB,
    parse_route,
    usage_from,
)
from sf_agent.types import AgentAnswer


# --- parse_route ---------------------------------------------------------------


def test_parse_route_reads_valid_route_and_reason():
    d = parse_route('{"route": "web", "reason": "Needs current info."}', has_history=False)
    assert d.route == ROUTE_WEB
    assert d.reason == "Needs current info."


def test_parse_route_tolerates_surrounding_prose():
    d = parse_route('Sure! {"route":"database","reason":"Ask the warehouse."} done', has_history=True)
    assert d.route == ROUTE_DATABASE


def test_parse_route_unknown_route_falls_back_to_database():
    d = parse_route('{"route": "carrier_pigeon", "reason": "nope"}', has_history=False)
    assert d.route == ROUTE_DATABASE


def test_parse_route_unparseable_falls_back_to_database():
    d = parse_route("not json at all", has_history=False)
    assert d.route == ROUTE_DATABASE


def test_parse_route_followup_without_history_downgrades():
    d = parse_route('{"route": "followup", "reason": "refers to prior"}', has_history=False)
    assert d.route == ROUTE_DATABASE
    assert "No prior results" in d.reason


def test_parse_route_followup_with_history_kept():
    d = parse_route('{"route": "followup", "reason": "refers to prior"}', has_history=True)
    assert d.route == ROUTE_FOLLOWUP


# --- usage_from ----------------------------------------------------------------


def test_usage_from_maps_all_token_categories():
    resp = SimpleNamespace(
        usage=SimpleNamespace(
            input_tokens=10,
            output_tokens=5,
            cache_read_input_tokens=3,
            cache_creation_input_tokens=2,
        )
    )
    assert usage_from(resp) == {"input": 10, "output": 5, "cache_read": 3, "cache_write": 2}


def test_usage_from_handles_missing_usage():
    assert usage_from(SimpleNamespace(usage=None)) == {
        "input": 0, "output": 0, "cache_read": 0, "cache_write": 0,
    }


# --- _merge_usage --------------------------------------------------------------


def test_merge_usage_folds_extra_and_recomputes_total_and_cost():
    ans = AgentAnswer(answer="x", value=None, values={}, executed_sql=[])
    ans.tokens = {"input": 100, "output": 50, "cache_read": 0, "cache_write": 0, "total": 150}
    _merge_usage(ans, {"input": 20, "output": 10, "cache_read": 0, "cache_write": 0}, "claude-sonnet-4-6")
    assert ans.tokens["input"] == 120
    assert ans.tokens["output"] == 60
    assert ans.tokens["total"] == 180
    # cost recomputed from folded totals (not stale)
    assert ans.cost_usd == round(120 / 1e6 * 3.0 + 60 / 1e6 * 15.0, 6)


# --- _extract_web --------------------------------------------------------------


def _text_block(text, citations=None):
    return SimpleNamespace(type="text", text=text, citations=citations or [])


def test_extract_web_concats_text_and_dedupes_citations_by_url():
    blocks = [
        _text_block(
            "First. ",
            [SimpleNamespace(title="Acme", url="https://a.com/x")],
        ),
        _text_block(
            "Second.",
            [
                SimpleNamespace(title="Acme dup", url="https://a.com/x"),  # dup url dropped
                SimpleNamespace(title="Beta", url="https://b.com/y"),
            ],
        ),
        SimpleNamespace(type="tool_use", text="ignored"),  # non-text ignored
    ]
    text, cites = _extract_web(blocks)
    assert text == "First. Second."
    assert cites == [
        {"title": "Acme", "url": "https://a.com/x"},
        {"title": "Beta", "url": "https://b.com/y"},
    ]


def test_extract_web_falls_back_to_url_when_title_missing():
    blocks = [_text_block("hi", [SimpleNamespace(title=None, url="https://c.com")])]
    _, cites = _extract_web(blocks)
    assert cites == [{"title": "https://c.com", "url": "https://c.com"}]


def test_extract_web_no_citations_returns_empty_list():
    text, cites = _extract_web([_text_block("just text")])
    assert text == "just text"
    assert cites == []
