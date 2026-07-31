"""Offline tests for the query router + the web/usage helpers on the agent.

These exercise pure logic (JSON parsing, usage folding, citation extraction) with no
network, so they run without Snowflake or Anthropic creds.
"""

import json
from types import SimpleNamespace

from sf_agent.agent import (
    _FINAL_ANSWER_INSTRUCTION,
    _INTERNAL_TURNS,
    _extract_web,
    _merge_usage,
    trim_history,
)
from sf_agent.prompts import FOLLOWUP_GUIDANCE
from sf_agent.router import (
    ROUTE_DATABASE,
    ROUTE_FOLLOWUP,
    ROUTE_WEB,
    conversation_digest,
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


# --- conversation digest: what the router is allowed to see ----------------------
# The router's job includes deciding whether a question "can be answered ENTIRELY from
# results already shown", but it used to receive only a has_history boolean — it never saw
# the results. So it spotted a follow-up only by wording, and a self-contained question
# ("Who is the CIO at Rivian?" right after the Rivian org chart was displayed) went back to
# the warehouse for data already on screen. The digest is what closes that gap.


def _text(text: str) -> SimpleNamespace:
    return SimpleNamespace(type="text", text=text)


def _answered(question: str, payload: dict) -> list[dict]:
    """One complete turn as converse() leaves it in the history."""
    return [
        {"role": "user", "content": question},
        {"role": "assistant", "content": [SimpleNamespace(type="tool_use", text=None)]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t", "content": "{}"}]},
        {"role": "assistant", "content": [_text(json.dumps(payload))]},
    ]


def test_digest_reports_questions_answers_and_available_data():
    history = _answered(
        "List the top 5 clients by active placements.",
        {"answer": "Google leads with 145.", "value": 145, "values": {"top_clients": [1, 2]}},
    )
    digest = conversation_digest(history)
    assert "List the top 5 clients by active placements." in digest
    assert "Google leads with 145." in digest
    assert "145" in digest  # the headline value the next question may ask about
    assert "top_clients" in digest  # so the router knows which rows are still on screen


def test_digest_omits_the_agents_own_scaffolding_turns():
    """FOLLOWUP_GUIDANCE and the final-answer instruction are appended as user turns; they
    are protocol chatter, and letting them through would read as things the user asked."""
    history = [
        {"role": "user", "content": "How many consultants?"},
        {"role": "user", "content": FOLLOWUP_GUIDANCE},
        {"role": "user", "content": _FINAL_ANSWER_INSTRUCTION},
        {"role": "assistant", "content": [_text('{"answer": "There are 22.", "value": 22}')]},
    ]
    digest = conversation_digest(history, internal_turns=_INTERNAL_TURNS)
    assert "How many consultants?" in digest
    assert "FOLLOW-UP question" not in digest
    assert "cannot call any more tools" not in digest


def test_digest_is_empty_until_something_has_been_answered():
    # Empty means "no history" to the caller, which then tells the router this is turn one.
    assert conversation_digest([]) == ""
    assert conversation_digest([{"role": "user", "content": "hi"}]) == ""
    # An assistant turn that only called a tool is not an answer the user saw.
    assert conversation_digest(
        [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": [SimpleNamespace(type="tool_use", text=None)]},
        ]
    ) == ""


def test_digest_keeps_only_the_most_recent_turns():
    history = []
    for i in range(10):
        history += _answered(f"question {i}", {"answer": f"answer {i}"})
    digest = conversation_digest(history, max_turns=3)
    assert "question 9" in digest and "question 8" in digest and "question 7" in digest
    assert "question 6" not in digest


def test_digest_survives_malformed_assistant_turns():
    history = [
        {"role": "user", "content": "q1"},
        {"role": "assistant", "content": [_text("I'll look that up.")]},  # prose, no JSON
        {"role": "user", "content": "q2"},
        {"role": "assistant", "content": [_text("{not json at all}")]},
        {"role": "user", "content": "q3"},
        {"role": "assistant", "content": [_text('{"answer": "real answer"}')]},
    ]
    digest = conversation_digest(history)
    assert "real answer" in digest and "q3" in digest
    assert "I'll look that up." not in digest


# --- history trimming: bound what each turn re-sends ------------------------------


def test_trim_history_keeps_recent_turns_and_never_orphans_a_tool_result():
    """Every turn re-sends the whole transcript, so it has to be bounded. The cut must land
    on a turn boundary: a tool_result separated from its tool_use is an API error."""
    history = []
    for i in range(8):
        history += _answered(f"question {i}", {"answer": f"answer {i}"})

    trimmed = trim_history(history, max_turns=3)
    texts = [m["content"] for m in trimmed if isinstance(m.get("content"), str)]
    assert texts == ["question 5", "question 6", "question 7"]
    # First message starts a turn, so no tool_result is left dangling.
    assert trimmed[0]["role"] == "user" and isinstance(trimmed[0]["content"], str)
    for i, m in enumerate(trimmed):
        content = m.get("content")
        if isinstance(content, list) and content and isinstance(content[0], dict) \
                and content[0].get("type") == "tool_result":
            prev = trimmed[i - 1]
            assert prev["role"] == "assistant", "tool_result must follow its tool_use"


def test_trim_history_leaves_a_short_conversation_untouched():
    history = _answered("only question", {"answer": "only answer"})
    assert trim_history(history, max_turns=12) is history
