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


class AgentError(RuntimeError):
    """Raised when the loop cannot produce a parseable final answer."""


def _tool_spec(tool: Tool) -> dict[str, Any]:
    return {"name": tool.name, "description": tool.description, "input_schema": tool.input_schema}


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
        messages: list[dict[str, Any]] = [{"role": "user", "content": question}]
        tool_specs = [_tool_spec(t) for t in self._tools.values()]
        executed_sql: list[str] = []

        # max_rounds query rounds + 1 final round where tools are withheld so the
        # model is forced to answer (guarantees termination).
        for round_i in range(self._config.max_rounds + 1):
            allow_tools = round_i < self._config.max_rounds

            kwargs: dict[str, Any] = {
                "model": self._config.model,
                "max_tokens": self._config.max_tokens,
                "system": SYSTEM_PROMPT,
                "messages": messages,
            }
            if allow_tools:
                kwargs["tools"] = tool_specs

            response = self._client.messages.create(**kwargs)
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
            data = _extract_json(text)
            answer = AgentAnswer(
                answer=str(data.get("answer", "")),
                value=data.get("value"),
                values=data.get("values") or {},
                executed_sql=executed_sql,
            )
            logger.info("agent answered value=%r after %d queries", answer.value, len(executed_sql))
            return answer

        raise AgentError(
            f"agent did not produce a final answer within {self._config.max_rounds} rounds"
        )
