"""The `answer` field is rendered verbatim, so machine formatting must never reach it.

Observed in production: the model prefixed its JSON with commentary, the whole blob failed to
parse, and the raw text — prose plus a full JSON object — became the visible answer. These
pin that JSON can no longer leak, whichever way it arrives.
"""

from sf_agent.agent import prose_only


def test_strips_prose_wrapped_json_and_keeps_the_inner_answer():
    """The exact shape seen in production: commentary, then the JSON envelope."""
    raw = (
        "I appreciate your question, but I need to be transparent about what our data can "
        'answer. {"answer": "MASTERSKILLLIST does not track Cisco initiatives.", '
        '"value": 0, "values": {"data_limitation": "none available"}, "chart": null}'
    )
    out = prose_only(raw)
    assert out == "MASTERSKILLLIST does not track Cisco initiatives."
    assert "{" not in out and '"value"' not in out


def test_recovers_answer_from_malformed_json():
    raw = 'Here you go: {"answer": "There are 83 active consultants at Cisco.", "value": 83,'
    out = prose_only(raw)
    assert out == "There are 83 active consultants at Cisco."


def test_strips_code_fences():
    raw = '```json\n{"answer": "Revenue was 18.44B USD.", "value": 18440000000}\n```'
    assert prose_only(raw) == "Revenue was 18.44B USD."


def test_unwraps_json_nested_inside_the_answer_field():
    """A model that nests a second envelope inside `answer` must still render as prose."""
    raw = '{"answer": "{\\"answer\\": \\"Cisco has 83 placements.\\", \\"value\\": 83}"}'
    assert prose_only(raw) == "Cisco has 83 placements."


def test_keeps_plain_prose_untouched_including_markdown():
    raw = "**Summary:** There are 83 active consultants.\n\n- Java: 40\n- Python: 20"
    assert prose_only(raw) == raw


def test_keeps_prose_when_json_has_no_usable_answer():
    raw = 'The query failed. {"error": "timeout"} Please retry.'
    out = prose_only(raw)
    assert "query failed" in out and "Please retry" in out
    assert "{" not in out


def test_empty_and_blank_are_safe():
    assert prose_only("") == ""
    assert prose_only("   \n  ") == ""


def test_malformed_json_path_preserves_unicode_not_mojibake():
    """The regex fallback must not corrupt non-ASCII text.

    Observed in production: answers arrived with 'â' where an em dash belonged, because the
    fallback did .encode().decode("unicode_escape") — that re-reads UTF-8 bytes as Latin-1,
    so '—' (3 bytes) became 'â' plus junk. Only this path was affected, which is why answers
    parsed by json.loads looked fine.
    """
    raw = 'Here: {"answer": "Two files — the RivianOrg section — excluded it.", "value": 1,'
    out = prose_only(raw)
    assert out == "Two files — the RivianOrg section — excluded it."
    assert "â" not in out


def test_malformed_json_path_handles_escapes_and_other_non_ascii():
    assert prose_only('x {"answer": "em dash \\u2014 here", "value": 1,') == "em dash — here"
    assert prose_only('x {"answer": "Café ✓ 6,000+ → 2,500", "value": 1,') == "Café ✓ 6,000+ → 2,500"
    # Escaped quotes and newlines survive the unescape.
    assert prose_only('x {"answer": "He said \\"hi\\"\\nnext", "value": 1,') == 'He said "hi"\nnext'
