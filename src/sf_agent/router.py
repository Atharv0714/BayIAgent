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
    client: Any, model: str, question: str, has_history: bool, max_tokens: int = 200
) -> tuple[RouteDecision, dict[str, int]]:
    """Classify ``question`` into a route. Returns the decision and its token usage.

    On any API failure we fall back to ``database`` rather than blocking the answer —
    routing is an enhancement, not a gate.
    """
    prompt = (
        f"Prior results available in this conversation: {'yes' if has_history else 'no'}.\n\n"
        f"Question: {question}"
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
    return parse_route(text, has_history), usage_from(resp)
