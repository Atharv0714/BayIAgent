"""Live web search for the "web" route, per provider.

The web route must be GROUNDED IN A REAL SEARCH or it is worse than useless: an uncited
answer from the model's training data reads exactly like a researched one while being
potentially years stale. That is precisely what happened on Z.AI — Anthropic's server-side
`web_search_20250305` tool is silently IGNORED there (no error, no citations, stop_reason
end_turn), so the agent answered company-strategy questions from memory.

Z.AI does support live search, but through its own native Completions API rather than the
Anthropic-compatible surface: a `web_search` tool that returns an answer plus a structured
result list (title / link / publish_date / refer). This module speaks that API and normalizes
the output to the same shape the Anthropic path produces, so the agent and UI are unchanged.
"""

from __future__ import annotations

import logging
import re
from typing import Any

import requests

logger = logging.getLogger("sf_agent.web_search")


class WebSearchError(RuntimeError):
    """Raised when a live search cannot be performed, so the caller can say so plainly
    instead of falling back to ungrounded recall."""


def zai_search_answer(
    *,
    api_key: str,
    model: str,
    url: str,
    messages: list[dict[str, str]],
    system: str,
    max_tokens: int,
    # 10 is deliberate, not arbitrary: measured against a real corporate-strategy question,
    # a 5-result search returned mostly social-media posts, while 10 surfaced the primary
    # sources (investor relations, the company site) alongside them. Retrieval breadth, not
    # the search_prompt, is what gets authoritative sources into the set — the prompt had no
    # measurable effect on which pages came back.
    max_results: int = 10,
    recency: str = "oneYear",
    timeout_s: float = 180.0,
) -> tuple[str, list[dict[str, str]], dict[str, int]]:
    """Answer `messages` with Z.AI's live web search.

    Returns (answer_text, citations, usage) where citations are ``{"title", "url"}`` dicts in
    reference order (so the answer's inline ``[n]`` markers line up) and usage maps onto the
    app's input/output token counters. Raises WebSearchError when the search cannot run —
    never returns an unsearched answer.
    """
    tool = {
        "type": "web_search",
        "web_search": {
            "enable": "True",
            "search_engine": "search-prime",
            "search_result": "True",
            "count": str(max_results),
            "content_size": "high",
            # Steer the retrieval itself toward authoritative sources. Without this the engine
            # happily returns Facebook/Instagram/LinkedIn posts for a corporate-strategy
            # question, which then become the answer's citations.
            "search_prompt": (
                "Prioritize authoritative, primary sources: the company's own newsroom, "
                "investor-relations and official site, regulatory filings, earnings-call "
                "coverage, and established business or trade press. Prefer the most recent "
                "material. Ignore social-media posts, reposts, forums, and marketing blogs "
                "unless nothing else covers the topic."
            ),
            "search_recency_filter": recency,
        },
    }
    payload = {
        "model": model,
        "messages": [{"role": "system", "content": system}, *messages],
        "tools": [tool],
        "max_tokens": max_tokens,
    }
    try:
        resp = requests.post(
            url,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json=payload,
            timeout=timeout_s,
        )
    except requests.RequestException as e:
        raise WebSearchError(f"could not reach the search API: {e}") from e

    if resp.status_code != 200:
        raise WebSearchError(f"search API returned HTTP {resp.status_code}: {resp.text[:200]}")

    try:
        data = resp.json()
    except ValueError as e:
        raise WebSearchError(f"search API returned non-JSON: {e}") from e

    choices = data.get("choices") or []
    if not choices:
        raise WebSearchError("search API returned no choices")
    message = choices[0].get("message") or {}
    text = _clean_answer(message.get("content") or "")

    citations = _citations_from(data, message)
    if not text:
        raise WebSearchError("search API returned an empty answer")
    # No results at all means the tool did not actually run; refuse rather than pass off
    # model recall as a researched answer.
    if not citations:
        raise WebSearchError("the search returned no sources")

    raw_usage = data.get("usage") or {}
    usage = {
        "input": int(raw_usage.get("prompt_tokens") or 0),
        "output": int(raw_usage.get("completion_tokens") or 0),
        "cache_read": 0,
        "cache_write": 0,
    }
    logger.info("zai web search: %d sources, %d chars", len(citations), len(text))
    return text, citations, usage


def _clean_answer(raw: str) -> str:
    """Strip the model's search narration and collapse the ragged whitespace it emits.

    GLM narrates its retrieval ("I'll search for...", "Search query: ...", "Based on my
    searches, here's what I found:") before the actual answer, and pads lines with stray
    indentation. None of that belongs in a user-facing answer, and the prompt alone doesn't
    reliably suppress it.
    """
    # GLM also emits pseudo-XML retrieval tags (<search>query</search>, <think>...</think>)
    # around its reasoning. Strip the tags and their contents before line filtering.
    cleaned = re.sub(
        r"<\s*(search|think|thinking|tool_call)\s*>.*?<\s*/\s*\1\s*>",
        " ",
        raw or "",
        flags=re.S | re.I,
    )
    # ...and any stray unclosed tag left behind.
    cleaned = re.sub(r"<\s*/?\s*(search|think|thinking|tool_call)\s*>", " ", cleaned, flags=re.I)
    lines = [ln.strip() for ln in cleaned.splitlines()]
    kept: list[str] = []
    prefixes = (
        "search query:",
        "i'll search",
        "i will search",
        "let me search",
        "searching for",
        "based on my search",
        "based on the search",
        "here's what i found",
        "here is what i found",
    )
    for ln in lines:
        low = ln.lower().lstrip("*_# ").rstrip(":. ")
        if any(low.startswith(p.rstrip(":")) for p in prefixes):
            continue
        kept.append(ln)
    # Collapse runs of blank lines left behind by the removals.
    out: list[str] = []
    for ln in kept:
        if not ln and (not out or not out[-1]):
            continue
        out.append(ln)
    return "\n".join(out).strip()


def _citations_from(data: dict[str, Any], message: dict[str, Any]) -> list[dict[str, str]]:
    """Normalize Z.AI's search results into {title, url}, de-duplicated, in reference order.

    The result list has appeared at the top level and on the message depending on the model,
    so check both. ``refer`` values look like "ref_3" and match the ``[3]`` markers in the
    answer text, so ordering by them keeps the prose and the source list consistent.
    """
    results: list[dict[str, Any]] = []
    for holder in (data, message):
        found = holder.get("web_search")
        if isinstance(found, list):
            results.extend(r for r in found if isinstance(r, dict))

    def refer_index(item: dict[str, Any]) -> int:
        raw = str(item.get("refer") or "")
        digits = "".join(ch for ch in raw if ch.isdigit())
        return int(digits) if digits else 10_000

    citations: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in sorted(results, key=refer_index):
        url = str(item.get("link") or item.get("url") or "").strip()
        if not url or url in seen:
            continue
        seen.add(url)
        title = str(item.get("title") or item.get("media") or url).strip()
        citations.append({"title": title, "url": url})
    return citations
