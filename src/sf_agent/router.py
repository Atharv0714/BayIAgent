"""Up-front query router: classify a question before the agent runs it.

Every question is tagged as one of four routes so the assistant is transparent about
how it answered — and so it can honor that choice:

- ``database``  -> query the Snowflake warehouse (grounded in live results)
- ``followup``  -> answer from the previous turn's already-fetched data
- ``web``       -> search the open internet (answer carries source citations)
- ``general``   -> answer from the assistant's own knowledge/reasoning (no data, no
                   search) — general knowledge, drafting, brainstorming, explanations

The classifier is a single small model call (biased toward ``database`` when a question
plausibly needs internal data, since that path is grounded). Kept in its own module so
``agent.py`` depends on a tiny, testable surface — ``classify`` and the route constants —
rather than inlining the routing logic into the tool loop that the evals exercise directly.
"""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel

from sf_agent.prompts import ROUTER_SYSTEM

ROUTE_DATABASE = "database"
ROUTE_FOLLOWUP = "followup"
ROUTE_WEB = "web"
ROUTE_GENERAL = "general"
_VALID_ROUTES = {ROUTE_DATABASE, ROUTE_FOLLOWUP, ROUTE_WEB, ROUTE_GENERAL}


class RouteDecision(BaseModel):
    """The router's verdict for one question: which path, and a one-line why."""

    route: str
    reason: str


# ── What the router is allowed to see of the conversation ───────────────────────────────
# The router used to receive only "prior results available: yes/no", yet its job includes
# deciding whether a question "can be answered ENTIRELY from results already shown". It
# could not: it never saw them. So it recognised a follow-up only by its wording, and any
# question phrased self-containedly ("Who is the CIO at Rivian?" right after the Rivian org
# chart was displayed) went back to the warehouse for data already on screen.
#
# It now gets a compact digest of what has been asked and answered. Bounded on purpose:
# enough to recognise the facts are already present, small enough that classification stays
# a cheap call.
_DIGEST_TURNS = 6
_DIGEST_ANSWER_CHARS = 400
_DIGEST_KEYS = 12


def _block_type(block: Any) -> str:
    return getattr(block, "type", None) or (block.get("type") if isinstance(block, dict) else "") or ""


def _block_text(block: Any) -> str:
    if isinstance(block, dict):
        return str(block.get("text") or "")
    return str(getattr(block, "text", "") or "")


def message_text(content: Any) -> str:
    """Flatten one message's content — a plain string or a list of SDK blocks — to text.

    Returns "" for tool_use / tool_result payloads, which carry no user-facing prose.
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(_block_text(b) for b in content if _block_type(b) == "text").strip()
    return ""


def _answer_summary(text: str) -> tuple[str, list[str]] | None:
    """Pull (prose, value-keys) out of an assistant turn that emitted the final JSON answer.

    Returns None for intermediate turns (tool calls, protocol stumbles) so the digest lists
    only what the user actually saw.
    """
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        data = json.loads(text[start : end + 1])
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(data, dict) or "answer" not in data:
        return None
    prose = str(data.get("answer") or "").strip()
    if data.get("value") is not None:
        prose = f"{prose} [headline value: {data['value']}]"
    values = data.get("values")
    keys = list(values)[:_DIGEST_KEYS] if isinstance(values, dict) else []
    return prose, keys


def conversation_digest(
    history: list[dict[str, Any]],
    internal_turns: frozenset[str] | set[str] = frozenset(),
    max_turns: int = _DIGEST_TURNS,
) -> str:
    """Summarize what this conversation has already established, for the router.

    ``internal_turns`` are the agent's own scaffolding messages (follow-up guidance, the
    final-answer instruction, the protocol reminder). They are appended to the history as
    ordinary user turns, so the caller — which owns those strings — passes them in to keep
    them out of the digest rather than having this module import from ``agent``.

    Returns "" when nothing has been answered yet, which the caller reads as "no history".
    """
    turns: list[str] = []
    pending_question = ""
    for msg in history:
        role = msg.get("role")
        text = message_text(msg.get("content"))
        if not text:
            continue
        if role == "user":
            if text.strip() not in internal_turns:
                pending_question = text.strip()
        elif role == "assistant":
            summary = _answer_summary(text)
            if summary is None:
                continue
            prose, keys = summary
            entry = f"Q: {pending_question[:240]}" if pending_question else "Q: (earlier question)"
            entry += f"\n   A: {prose[:_DIGEST_ANSWER_CHARS]}"
            if keys:
                entry += f"\n   data still on screen: {', '.join(keys)}"
            turns.append(entry)
            pending_question = ""
    if not turns:
        return ""
    recent = turns[-max_turns:]
    return "\n".join(f"[{i}] {t}" for i, t in enumerate(recent, 1))


def usage_from(response: Any) -> dict[str, int]:
    """Pull token counts off a Messages response into our flat usage dict.

    Shared with the agent loop so every model call — router, tool loop, web search —
    is metered the same way and rolled into the answer's diagnostics.
    """
    u = getattr(response, "usage", None)
    if u is None:
        return {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0}
    return {
        "input": getattr(u, "input_tokens", 0) or 0,
        "output": getattr(u, "output_tokens", 0) or 0,
        "cache_read": getattr(u, "cache_read_input_tokens", 0) or 0,
        "cache_write": getattr(u, "cache_creation_input_tokens", 0) or 0,
    }


def parse_route(text: str, has_history: bool) -> RouteDecision:
    """Extract {route, reason} from the classifier's reply, defensively.

    Anything unparseable, an unknown route, or a "followup" with no prior results falls
    back to ``database`` — the always-grounded path — so a bad classification never
    routes a real data question to the wrong place.
    """
    route = ROUTE_DATABASE
    reason = "Defaulted to a warehouse query."
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        try:
            data = json.loads(text[start : end + 1])
            r = str(data.get("route", "")).strip().lower()
            if r in _VALID_ROUTES:
                route = r
            reason = str(data.get("reason") or reason).strip()
        except (json.JSONDecodeError, ValueError):
            pass
    # A follow-up needs something to follow up on; without history it's really a fresh
    # database question.
    if route == ROUTE_FOLLOWUP and not has_history:
        return RouteDecision(route=ROUTE_DATABASE, reason="No prior results to follow up on.")
    return RouteDecision(route=route, reason=reason)


def classify(
    client: Any, model: str, question: str, digest: str = "", max_tokens: int = 200
) -> tuple[RouteDecision, dict[str, int]]:
    """Classify ``question`` into a route. Returns the decision and its token usage.

    ``digest`` is ``conversation_digest(history)`` — what has already been asked and
    answered. Passing the real content, rather than a "history: yes/no" flag, is what lets
    the router see that a question's answer is already on screen and route it to
    ``followup`` instead of paying for another warehouse round-trip.

    On any API failure we fall back to ``database`` rather than blocking the answer —
    routing is an enhancement, not a gate.
    """
    prompt = (
        (
            "Already asked and answered in this conversation (most recent last):\n"
            f"{digest}\n\n"
            if digest
            else "This is the first question in the conversation; nothing has been answered yet.\n\n"
        )
        + f"Latest question: {question}"
    )
    try:
        resp = client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=ROUTER_SYSTEM,
            messages=[{"role": "user", "content": prompt}],
        )
    except Exception:  # noqa: BLE001 — never let routing failure block the answer
        return RouteDecision(route=ROUTE_DATABASE, reason="Router unavailable; queried the warehouse."), {
            "input": 0, "output": 0, "cache_read": 0, "cache_write": 0,
        }
    text = "".join(b.text for b in resp.content if getattr(b, "type", None) == "text")
    return parse_route(text, bool(digest)), usage_from(resp)
