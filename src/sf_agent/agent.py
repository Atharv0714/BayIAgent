from __future__ import annotations

import json
import logging
from typing import Any

import anthropic

from sf_agent.config import AgentConfig
from sf_agent.prompts import SYSTEM_PROMPT
from sf_agent.tools.base import Tool
from sf_agent.types import AgentAnswer

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


def _tool_spec(tool: Tool) -> dict[str, Any]:
    return {"name": tool.name, "description": tool.description, "input_schema": tool.input_schema}


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
        self._client = client or anthropic.Anthropic(api_key=config.api_key)

    def ask(self, question: str) -> AgentAnswer:
        """Single-shot: answer `question` with no prior context."""
        answer, _ = self.converse([], question)
        return answer

    def converse(
        self, history: list[dict[str, Any]], question: str
    ) -> tuple[AgentAnswer, list[dict[str, Any]]]:
        """Answer `question` in the context of a prior message `history`.

        Returns the answer and the full updated message list (including this turn's
        tool calls, tool results, and the final assistant reply). Pass that list
        back as `history` on the next turn to keep the conversation — and the data
        rows already fetched — in context, so a follow-up can analyze earlier output
        without re-querying. The message list holds the SDK's own content blocks, so
        keep it server-side rather than serializing it.
        """
        messages: list[dict[str, Any]] = list(history)
        messages.append({"role": "user", "content": question})
        tool_specs = [_tool_spec(t) for t in self._tools.values()]
        # Cache the tool specs (they never change across rounds) by marking the last one.
        cached_tool_specs = [dict(s) for s in tool_specs]
        if cached_tool_specs:
            cached_tool_specs[-1] = {**cached_tool_specs[-1], "cache_control": dict(_CACHE_CONTROL)}
        # System prompt is identical every round — cache it too.
        cached_system = [
            {"type": "text", "text": SYSTEM_PROMPT, "cache_control": dict(_CACHE_CONTROL)}
        ]
        executed_sql: list[str] = []
        usage = {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0}

        # max_rounds query rounds + 1 final round where tools are withheld so the
        # model is forced to answer (guarantees termination).
        for round_i in range(self._config.max_rounds + 1):
            allow_tools = round_i < self._config.max_rounds

            # On the final (tool-withheld) round, explicitly demand the JSON answer;
            # otherwise the model tends to summarize in prose and lose the value.
            if not allow_tools:
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
            u = getattr(response, "usage", None)
            if u is not None:
                usage["input"] += getattr(u, "input_tokens", 0) or 0
                usage["output"] += getattr(u, "output_tokens", 0) or 0
                usage["cache_read"] += getattr(u, "cache_read_input_tokens", 0) or 0
                usage["cache_write"] += getattr(u, "cache_creation_input_tokens", 0) or 0
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
