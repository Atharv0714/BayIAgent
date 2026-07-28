from __future__ import annotations

import json
import logging
import re
import time
from typing import Any
from urllib.parse import urlparse

import anthropic

from sf_agent.config import AgentConfig
from sf_agent.prompts import FOLLOWUP_GUIDANCE, GENERAL_SYSTEM, SYSTEM_PROMPT, WEB_SYSTEM
from sf_agent.router import ROUTE_FOLLOWUP, ROUTE_GENERAL, ROUTE_WEB, classify, usage_from
from sf_agent.tools.base import Tool
from sf_agent.types import AgentAnswer, ToolResult

logger = logging.getLogger("sf_agent.agent")

# Sent back when the model narrates without calling a tool or emitting the final
# JSON, to pull it back onto the protocol instead of failing the turn.
_PROTOCOL_REMINDER = (
    "Do not narrate your next step. Either call a tool now to fetch the data you "
    "need, or, if you already have it, reply with ONLY the final JSON object described "
    "in the system prompt — no prose, no code fences."
)

# Injected on the final round, where tools are withheld to force termination. Without
# this the model tends to summarize its findings in prose (which _extract_json then
# rejects, yielding value=None); this makes the JSON-only contract explicit at the
# one moment it matters most.
_FINAL_ANSWER_INSTRUCTION = (
    "You cannot call any more tools. Using only the data already gathered above, reply "
    "now with ONLY the final JSON object described in the system prompt — no prose, no "
    "code fences. Put any list of names or supporting figures inside \"values\"; keep "
    "\"answer\" to one or two sentences."
)


class AgentError(RuntimeError):
    """Raised when the loop cannot produce a parseable final answer."""


# Prompt-caching marker. The system prompt and tool specs are byte-identical on
# every round, and the message history (including the large row payloads returned
# by earlier queries) only grows — so without caching each round re-bills the
# entire accumulated prefix in full, which is what drove one question past 100k
# tokens. Marking the static prefix + a rolling breakpoint on the conversation
# lets every round after the first read that prefix from cache at ~10% of the cost.
_CACHE_CONTROL = {"type": "ephemeral"}


# List pricing, USD per million tokens, used only for the UI's estimated per-answer cost.
# Picked by model family so the estimate tracks the provider actually in use (Anthropic vs
# Z.AI GLM). Both are approximations for any other model; the real bill is the provider's.
_ANTHROPIC_PRICE = {"input": 3.0, "output": 15.0, "cache_read": 0.30, "cache_write": 3.75}
# Z.AI GLM pricing (approx — based on the published GLM-4.x line; confirm current GLM-5.2
# rates on z.ai). Cache read/write are estimates: GLM's cache pricing on the
# Anthropic-compatible endpoint isn't separately published, so these are set conservatively
# (no large discount assumed) rather than understating cost.
_GLM_PRICE = {"input": 0.60, "output": 2.20, "cache_read": 0.11, "cache_write": 0.60}


def _price_for_model(model: str) -> dict[str, float]:
    """The per-million-token price table for the configured model's family."""
    return _GLM_PRICE if str(model).lower().startswith("glm") else _ANTHROPIC_PRICE

# Pull the table after FROM/JOIN — either a quoted identifier ("Billable Data ...")
# or a bare dotted name (schema.table) — to report which sources an answer drew from.
_TABLE_RE = re.compile(r'\b(?:FROM|JOIN)\s+("[^"]+"|[A-Za-z_][\w$.]*)', re.IGNORECASE)

# A `SELECT * ... LIMIT <=5` is the agent peeking at a table's shape, not the query
# that produced the answer — excluded from the reported source for that reason.
_SELECT_STAR_RE = re.compile(r"select\s+\*", re.IGNORECASE)
_LIMIT_RE = re.compile(r"\blimit\s+(\d+)", re.IGNORECASE)


def _is_discovery(sql: str) -> bool:
    if not _SELECT_STAR_RE.search(sql or ""):
        return False
    m = _LIMIT_RE.search(sql)
    return bool(m) and int(m.group(1)) <= 5


# A schema peek (`SELECT * ... LIMIT <=5`) exists only to reveal a table's columns so
# the model can write the real query — the answer is never grounded in it. But it is the
# widest-possible payload (all columns) and, like every tool result, is re-sent on every
# later round of the loop. Keeping the columns plus a couple of sample rows preserves
# everything the peek is actually used for (column names + example value shapes) while
# dropping the redundant remainder, so this trims tokens without touching answer data.
_DISCOVERY_SAMPLE_ROWS = 2


def _compact_discovery_content(result: ToolResult) -> str:
    r = result.result
    if r is None:  # defensive: ok results always carry rows
        return result.to_model_text()
    kept = min(len(r.rows), _DISCOVERY_SAMPLE_ROWS)
    return json.dumps(
        {
            "columns": r.columns,
            "rows": r.rows[:_DISCOVERY_SAMPLE_ROWS],
            "row_count": r.row_count,
            "truncated": r.truncated,
            "note": f"schema peek: {kept} of {len(r.rows)} sample rows shown",
        },
        default=str,
    )


def _tool_spec(tool: Tool) -> dict[str, Any]:
    return {"name": tool.name, "description": tool.description, "input_schema": tool.input_schema}


def _estimate_cost(usage: dict[str, int], model: str) -> float:
    price = _price_for_model(model)
    return round(sum(usage.get(k, 0) / 1_000_000 * p for k, p in price.items()), 6)


def _extract_sources(sqls: list[str]) -> list[str]:
    """Distinct data tables referenced across the executed SQL, in first-seen order.

    Catalog probes (information_schema) and shape-peeking `SELECT * LIMIT 5` queries
    are omitted so the reported source is the actual data the answer drew from, not
    the tables the agent browsed to find it.
    """
    seen: list[str] = []
    for sql in sqls:
        if _is_discovery(sql):
            continue
        for m in _TABLE_RE.finditer(sql or ""):
            name = m.group(1).strip('"')
            if "information_schema" in name.lower():
                continue
            if name not in seen:
                seen.append(name)
    return seen


def _merge_usage(answer: AgentAnswer, extra: dict[str, int], model: str) -> None:
    """Fold an extra call's token usage (e.g. the router) into an answer's diagnostics,
    then recompute the total and estimated cost so the panel stays honest."""
    for k in ("input", "output", "cache_read", "cache_write"):
        answer.tokens[k] = answer.tokens.get(k, 0) + extra.get(k, 0)
    answer.tokens["total"] = sum(v for k, v in answer.tokens.items() if k != "total")
    answer.cost_usd = _estimate_cost(answer.tokens, model)


def _extract_web(content: list[Any]) -> tuple[str, list[dict[str, str]]]:
    """Pull the answer text and de-duplicated source citations from a web-search reply.

    Text blocks carry `.citations`; each web citation exposes a title + url. We keep the
    first occurrence of each url so the UI can cite where the internet facts came from.
    """
    parts: list[str] = []
    cites: list[dict[str, str]] = []
    seen: set[str] = set()
    for block in content:
        if getattr(block, "type", None) != "text":
            continue
        parts.append(block.text)
        for c in getattr(block, "citations", None) or []:
            url = getattr(c, "url", None)
            if url and url not in seen:
                seen.add(url)
                cites.append({"title": (getattr(c, "title", None) or url), "url": url})
    return "".join(parts).strip(), cites


def _mark_cache_breakpoint(messages: list[dict[str, Any]]) -> None:
    """Put a single rolling cache breakpoint on the last message.

    Everything before the breakpoint — system prompt, tools, and every prior
    turn's fetched rows — is served from cache on the next round instead of being
    re-billed. We clear any earlier message-level breakpoint first so the request
    never exceeds Anthropic's 4-breakpoint limit (system + tools already use two).
    Only dict/str content is touched; assistant turns hold SDK block objects and
    are always followed by a user message, so the last message is never one of them.
    """
    for m in messages:
        content = m.get("content")
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict):
                    block.pop("cache_control", None)
    last = messages[-1]
    content = last["content"]
    if isinstance(content, str):
        last["content"] = [
            {"type": "text", "text": content, "cache_control": dict(_CACHE_CONTROL)}
        ]
    elif isinstance(content, list) and content and isinstance(content[-1], dict):
        content[-1]["cache_control"] = dict(_CACHE_CONTROL)


def _extract_json(text: str) -> dict[str, Any]:
    """Pull the outermost JSON object out of the model's final message.

    Spans from the first '{' to the last '}', so stray prose or code fences around
    the object don't break parsing.
    """
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end <= start:
        raise AgentError(f"final answer was not JSON: {text!r}")
    try:
        return json.loads(text[start : end + 1])
    except json.JSONDecodeError as e:
        raise AgentError(f"could not parse final answer JSON: {e}; raw={text!r}") from e


class SnowflakeAgent:
    """Anthropic tool-use loop over the chunk-1 Tool protocol.

    The model writes SQL and calls a tool; we execute it (through the guard) and feed
    the result back, up to `max_rounds` query rounds, then the model answers. The loop
    dispatches purely by tool name, so registering a `cortex_analyst` Tool alongside
    `run_sql` later needs no change here.
    """

    def __init__(
        self,
        tools: list[Tool],
        config: AgentConfig,
        client: anthropic.Anthropic | None = None,
    ) -> None:
        self._tools: dict[str, Tool] = {t.name: t for t in tools}
        self._config = config
        # base_url=None keeps the SDK's default (Anthropic); a Z.AI base URL routes the
        # same Messages calls to GLM. One switch moves the whole loop between providers.
        self._client = client or anthropic.Anthropic(
            api_key=config.api_key, base_url=config.base_url
        )

    def ask(self, question: str) -> AgentAnswer:
        """Single-shot: answer `question` with no prior context."""
        answer, _ = self.converse([], question)
        return answer

    def _attach_diag(
        self, answer: AgentAnswer, start: float, usage: dict[str, int], source_sql: list[str]
    ) -> AgentAnswer:
        """Record timing, token usage/cost, and data sources onto the answer."""
        answer.elapsed_ms = (time.perf_counter() - start) * 1000
        answer.tokens = {**usage, "total": sum(usage.values())}
        answer.cost_usd = _estimate_cost(usage, self._config.model)
        answer.sources = _extract_sources(source_sql)
        return answer

    def converse(
        self,
        history: list[dict[str, Any]],
        question: str,
        guidance: str | None = None,
        system: str = SYSTEM_PROMPT,
        use_tools: bool = True,
        attachments: list[dict[str, Any]] | None = None,
    ) -> tuple[AgentAnswer, list[dict[str, Any]]]:
        """Answer `question` in the context of a prior message `history`.

        Returns the answer and the full updated message list (including this turn's
        tool calls, tool results, and the final assistant reply). Pass that list
        back as `history` on the next turn to keep the conversation — and the data
        rows already fetched — in context, so a follow-up can analyze earlier output
        without re-querying. The message list holds the SDK's own content blocks, so
        keep it server-side rather than serializing it.

        `guidance`, when set, is appended as an extra user turn before the loop runs —
        the router uses it to nudge a follow-up toward answering from context.

        `system` overrides the system prompt (e.g. GENERAL_SYSTEM for the general lane).
        `use_tools=False` runs the model with NO query tools — a single-shot direct
        answer — reusing the same JSON contract, diagnostics, and recovery. Both keep
        the default (database) call path byte-for-byte unchanged.

        `attachments`, when set, are Anthropic content blocks for an uploaded file the
        user attached (a document to read); they ride alongside the question text.
        """
        start = time.perf_counter()
        messages: list[dict[str, Any]] = list(history)
        if attachments:
            messages.append(
                {"role": "user", "content": [{"type": "text", "text": question}, *attachments]}
            )
        else:
            messages.append({"role": "user", "content": question})
        if guidance:
            messages.append({"role": "user", "content": guidance})
        # No query tools in general mode; otherwise advertise them (cache the last spec).
        tool_specs = [_tool_spec(t) for t in self._tools.values()] if use_tools else []
        cached_tool_specs = [dict(s) for s in tool_specs]
        if cached_tool_specs:
            cached_tool_specs[-1] = {**cached_tool_specs[-1], "cache_control": dict(_CACHE_CONTROL)}
        # System prompt is identical every round — cache it too.
        cached_system = [
            {"type": "text", "text": system, "cache_control": dict(_CACHE_CONTROL)}
        ]
        executed_sql: list[str] = []
        # Only successful, non-discovery queries — used to report the real data source
        # (so a failed table probe or a `SELECT * LIMIT 5` peek isn't shown as a source).
        source_sql: list[str] = []
        usage = {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0}

        # max_rounds query rounds + 1 final round where tools are withheld so the
        # model is forced to answer (guarantees termination).
        for round_i in range(self._config.max_rounds + 1):
            allow_tools = use_tools and round_i < self._config.max_rounds

            # On the final (tool-withheld) round of a TOOL loop, explicitly demand the JSON
            # answer; otherwise the model tends to summarize in prose and lose the value.
            # Skipped in general mode (use_tools=False): its system prompt already asks for
            # JSON, and the "using only the data gathered above" wording would be wrong.
            if not allow_tools and use_tools:
                messages.append({"role": "user", "content": _FINAL_ANSWER_INSTRUCTION})

            # Roll the conversation cache breakpoint to the current last message so
            # the whole prefix before it is a cache hit on this round.
            _mark_cache_breakpoint(messages)

            kwargs: dict[str, Any] = {
                "model": self._config.model,
                "max_tokens": self._config.max_tokens,
                "system": cached_system,
                "messages": messages,
            }
            if allow_tools:
                kwargs["tools"] = cached_tool_specs

            response = self._client.messages.create(**kwargs)
            for k, v in usage_from(response).items():
                usage[k] += v
            messages.append({"role": "assistant", "content": response.content})

            tool_uses = [b for b in response.content if getattr(b, "type", None) == "tool_use"]
            if allow_tools and response.stop_reason == "tool_use" and tool_uses:
                tool_results = []
                for tu in tool_uses:
                    tool = self._tools.get(tu.name)
                    if tool is None:
                        content = json.dumps({"error": "unknown_tool", "message": tu.name})
                        is_error = True
                    else:
                        result = tool.run(**tu.input)
                        if result.executed_sql:
                            executed_sql.append(result.executed_sql)
                            if result.ok:
                                source_sql.append(result.executed_sql)
                        # A schema peek is only for column discovery; trim its sample so
                        # the widest payload in the loop isn't re-sent in full each round.
                        if result.ok and _is_discovery(result.executed_sql or ""):
                            content = _compact_discovery_content(result)
                        else:
                            content = result.to_model_text()
                        is_error = not result.ok
                    tool_results.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": tu.id,
                            "content": content,
                            "is_error": is_error,
                        }
                    )
                messages.append({"role": "user", "content": tool_results})
                continue

            text = "".join(b.text for b in response.content if getattr(b, "type", None) == "text")
            try:
                data = _extract_json(text)
            except AgentError:
                # The model replied with prose but neither called a tool nor emitted
                # the final JSON (e.g. "Let me get the list:" then stopped, or asked a
                # clarifying question). If rounds remain, nudge it back on protocol and
                # let it recover; on the final (tool-withheld) round, surface the prose
                # as a best-effort answer rather than 502-ing the caller.
                if allow_tools:
                    messages.append({"role": "user", "content": _PROTOCOL_REMINDER})
                    continue
                answer = AgentAnswer(
                    answer=text.strip() or "The agent could not produce an answer.",
                    value=None,
                    values={},
                    executed_sql=executed_sql,
                )
                self._attach_diag(answer, start, usage, source_sql)
                logger.info(
                    "agent returned non-JSON prose after %d queries; tokens=%s",
                    len(executed_sql), usage,
                )
                return answer, messages

            chart = data.get("chart")
            answer = AgentAnswer(
                answer=str(data.get("answer", "")),
                value=data.get("value"),
                values=data.get("values") or {},
                executed_sql=executed_sql,
                chart=chart if isinstance(chart, dict) else None,
            )
            self._attach_diag(answer, start, usage, source_sql)
            logger.info(
                "agent answered value=%r chart=%s after %d queries; tokens=%s",
                answer.value,
                answer.chart.get("type") if answer.chart else None,
                len(executed_sql),
                usage,
            )
            return answer, messages

        raise AgentError(
            f"agent did not produce a final answer within {self._config.max_rounds} rounds"
        )

    def route_and_answer(
        self,
        history: list[dict[str, Any]],
        question: str,
        attachments: list[dict[str, Any]] | None = None,
    ) -> tuple[AgentAnswer, list[dict[str, Any]]]:
        """Classify the question, then answer it via the chosen route.

        This is the entry point the web UI uses (the evals still call `converse`
        directly, so their grounded-SQL behavior is unchanged). The route — database,
        followup, web, or general — plus the router's one-line reason are recorded on the
        answer so the UI can show how each question was handled, and the router's token cost
        is folded into the answer's diagnostics.
        """
        decision, router_usage = classify(
            self._client, self._config.model, question, bool(history)
        )
        if decision.route == ROUTE_WEB:
            answer, updated = self._answer_web(history, question)
        elif decision.route == ROUTE_GENERAL:
            # Ordinary-assistant lane: answer from the model's own knowledge, no query
            # tools, using the general system prompt (still the JSON contract, so decks work).
            answer, updated = self.converse(
                history, question, system=GENERAL_SYSTEM, use_tools=False, attachments=attachments
            )
        else:
            guidance = FOLLOWUP_GUIDANCE if decision.route == ROUTE_FOLLOWUP else None
            answer, updated = self.converse(
                history, question, guidance=guidance, attachments=attachments
            )

        answer.route = decision.route
        answer.route_reason = decision.reason
        _merge_usage(answer, router_usage, self._config.model)
        logger.info("routed q=%r -> %s (%s)", question, decision.route, decision.reason)
        return answer, updated

    def _answer_web(
        self, history: list[dict[str, Any]], question: str
    ) -> tuple[AgentAnswer, list[dict[str, Any]]]:
        """Answer from the open internet via the SDK's server-side web_search tool.

        Unlike the database paths this returns cited prose (not the JSON contract); the
        tool attaches source citations, which we surface so every internet fact is
        traceable. History is persisted as plain text turns (not the raw server-tool
        blocks), so a later database turn can replay the conversation without needing
        the web tool re-declared.
        """
        start = time.perf_counter()
        usage = {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0}
        messages: list[dict[str, Any]] = list(history)
        # Drop any cache markers carried in from a prior turn's history so we stay within
        # Anthropic's 4-breakpoint limit, then anchor one breakpoint on this question. A
        # single breakpoint caches the whole prefix before it (tools + system + history +
        # question); each `pause_turn` round-trip only appends assistant turns after it, so
        # the accumulated search results are re-read from cache instead of re-billed in full.
        for m in messages:
            content = m.get("content")
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict):
                        block.pop("cache_control", None)
        messages.append(
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": question, "cache_control": dict(_CACHE_CONTROL)}
                ],
            }
        )
        web_tool = [
            {
                "type": "web_search_20250305",
                "name": "web_search",
                "max_uses": self._config.web_search_max_uses,
            }
        ]

        final_content: list[Any] = []
        try:
            # The search can span several server turns; `pause_turn` means "keep going".
            for _ in range(6):
                response = self._client.messages.create(
                    model=self._config.model,
                    max_tokens=self._config.max_tokens,
                    system=WEB_SYSTEM,
                    messages=messages,
                    tools=web_tool,
                )
                for k, v in usage_from(response).items():
                    usage[k] += v
                messages.append({"role": "assistant", "content": response.content})
                final_content = response.content
                if response.stop_reason != "pause_turn":
                    break
        except Exception as e:  # noqa: BLE001 — web search may be disabled on the account
            answer = AgentAnswer(
                answer=f"Internet search is unavailable right now ({e}).",
                value=None,
                executed_sql=[],
            )
            answer.elapsed_ms = (time.perf_counter() - start) * 1000
            answer.tokens = {**usage, "total": sum(usage.values())}
            answer.cost_usd = _estimate_cost(usage, self._config.model)
            logger.warning("web route failed q=%r err=%s", question, e)
            return answer, history  # don't persist a broken turn

        text, citations = _extract_web(final_content)
        domains: list[str] = []
        for c in citations:
            host = urlparse(c["url"]).netloc or c["url"]
            if host not in domains:
                domains.append(host)

        answer = AgentAnswer(
            answer=text or "The web search returned no usable answer.",
            value=None,
            values={},
            executed_sql=[],
            chart=None,
            citations=citations or None,
            sources=domains,
        )
        answer.elapsed_ms = (time.perf_counter() - start) * 1000
        answer.tokens = {**usage, "total": sum(usage.values())}
        answer.cost_usd = _estimate_cost(usage, self._config.model)

        # Persist compact text turns so follow-ups keep context without the raw
        # server-tool blocks (which can't be replayed to a call that lacks the tool).
        updated = list(history)
        updated.append({"role": "user", "content": question})
        updated.append({"role": "assistant", "content": text or "(no answer)"})
        logger.info("web answered chars=%d citations=%d", len(text), len(citations))
        return answer, updated
