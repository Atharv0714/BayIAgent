"""Z.AI live-search normalization: narration stripping and citation ordering.

The web route exists to be GROUNDED. Two things must hold: the user never sees the model's
retrieval narration, and every answer carries real sources in reference order so the inline
[n] markers line up. A search that returns no sources must raise rather than pass model
recall off as researched — that silent failure is what made this path untrustworthy on GLM.
"""

import pytest

from sf_agent.web_search import WebSearchError, _citations_from, _clean_answer, zai_search_answer


def test_clean_answer_strips_search_narration():
    raw = (
        "I'll search for current information about Cisco.\n"
        "  \n"
        "Search query: \"Cisco initiatives 2026\"\n"
        "Based on my searches, here's what I found:\n"
        "\n"
        "Cisco is focused on AI infrastructure and security.\n"
    )
    out = _clean_answer(raw)
    assert out == "Cisco is focused on AI infrastructure and security."


def test_clean_answer_keeps_real_content_and_collapses_blank_runs():
    out = _clean_answer("Summary line.\n\n\n\nDetail line.")
    assert out == "Summary line.\n\nDetail line."


def test_citations_follow_reference_order_and_dedupe():
    data = {
        "web_search": [
            {"refer": "ref_3", "title": "Third", "link": "https://c.example"},
            {"refer": "ref_1", "title": "First", "link": "https://a.example"},
            {"refer": "ref_2", "title": "Second", "link": "https://b.example"},
            {"refer": "ref_9", "title": "Dup", "link": "https://a.example"},
        ]
    }
    cites = _citations_from(data, {})
    # Ordered by refer so the answer's [n] markers match, with the duplicate URL dropped.
    assert [c["url"] for c in cites] == ["https://a.example", "https://b.example", "https://c.example"]
    assert cites[0]["title"] == "First"


def test_citations_also_read_from_the_message_payload():
    msg = {"web_search": [{"refer": "ref_1", "title": "OnMessage", "link": "https://m.example"}]}
    assert _citations_from({}, msg)[0]["title"] == "OnMessage"


def test_search_without_sources_raises_instead_of_answering(monkeypatch):
    """An answer with no sources means the tool didn't really run — refuse it."""

    class _Resp:
        status_code = 200

        @staticmethod
        def json():
            return {"choices": [{"message": {"content": "Cisco does many things."}}]}

    monkeypatch.setattr("sf_agent.web_search.requests.post", lambda *a, **k: _Resp())
    with pytest.raises(WebSearchError, match="no sources"):
        zai_search_answer(
            api_key="k", model="glm-5.2", url="https://x", messages=[{"role": "user", "content": "q"}],
            system="s", max_tokens=100,
        )


def test_http_error_raises_web_search_error(monkeypatch):
    class _Resp:
        status_code = 401
        text = "unauthorized"

        @staticmethod
        def json():
            return {}

    monkeypatch.setattr("sf_agent.web_search.requests.post", lambda *a, **k: _Resp())
    with pytest.raises(WebSearchError, match="HTTP 401"):
        zai_search_answer(
            api_key="k", model="glm-5.2", url="https://x", messages=[{"role": "user", "content": "q"}],
            system="s", max_tokens=100,
        )
