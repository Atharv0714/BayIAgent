"""Offline tests for the agent loop — no Snowflake or Anthropic creds needed.

A stub Anthropic client replays scripted responses; a stub tool stands in for
run_sql. These verify dispatch, executed_sql capture, JSON parsing, and the cap.
"""

from types import SimpleNamespace
from typing import Any

import pytest

from sf_agent.agent import SnowflakeAgent
from sf_agent.config import AgentConfig
from sf_agent.types import QueryResult, ToolResult


def _text_block(text: str) -> SimpleNamespace:
    return SimpleNamespace(type="text", text=text)


def _tool_use_block(tool_id: str, name: str, tool_input: dict[str, Any]) -> SimpleNamespace:
    return SimpleNamespace(type="tool_use", id=tool_id, name=name, input=tool_input)


def _response(content: list[SimpleNamespace], stop_reason: str) -> SimpleNamespace:
    return SimpleNamespace(content=content, stop_reason=stop_reason)


class StubMessages:
    def __init__(self, scripted: list[SimpleNamespace]) -> None:
        self._scripted = scripted
        self.calls: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> SimpleNamespace:
        # Snapshot the messages list: the loop mutates the same list by reference,
        # so we copy it to freeze what was present at this call.
        self.calls.append({**kwargs, "messages": list(kwargs["messages"])})
        if not self._scripted:
            # Model keeps being asked but ran out of script: emulate "always tool_use".
            return _response([_tool_use_block("loop", "run_sql", {"query": "SELECT 1"})], "tool_use")
        return self._scripted.pop(0)


class StubClient:
    def __init__(self, scripted: list[SimpleNamespace]) -> None:
        self.messages = StubMessages(scripted)


class StubRunSql:
    name = "run_sql"
    description = "stub"
    input_schema = {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}

    def __init__(self) -> None:
        self.calls: list[str] = []

    def run(self, query: str, max_rows: int | None = None) -> ToolResult:
        self.calls.append(query)
        return ToolResult(
            ok=True,
            executed_sql=query,
            elapsed_ms=1.0,
            result=QueryResult(columns=["N"], rows=[[42]], row_count=1, truncated=False),
        )


def _config(max_rounds: int = 5) -> AgentConfig:
    return AgentConfig(api_key="test-key", max_rounds=max_rounds)  # type: ignore[call-arg]


def test_dispatches_tool_then_parses_answer() -> None:
    scripted = [
        _response([_tool_use_block("t1", "run_sql", {"query": "SELECT COUNT(*) FROM t"})], "tool_use"),
        _response([_text_block('{"answer": "There are 42 rows.", "value": 42, "values": {}}')], "end_turn"),
    ]
    client = StubClient(scripted)
    tool = StubRunSql()
    agent = SnowflakeAgent(tools=[tool], config=_config(), client=client)

    answer = agent.ask("How many rows are there?")

    assert answer.value == 42
    assert answer.answer == "There are 42 rows."
    assert tool.calls == ["SELECT COUNT(*) FROM t"]
    assert answer.executed_sql == ["SELECT COUNT(*) FROM t"]
    # one tool round + one final round
    assert len(client.messages.calls) == 2
    # tools advertised on the first call, withheld is not relevant here (it answered)
    assert "tools" in client.messages.calls[0]


def test_parses_answer_wrapped_in_prose_and_fences() -> None:
    messy = 'Here is the result:\n```json\n{"answer": "ok", "value": 7, "values": {"a": 1}}\n```'
    client = StubClient([_response([_text_block(messy)], "end_turn")])
    agent = SnowflakeAgent(tools=[StubRunSql()], config=_config(), client=client)

    answer = agent.ask("q")

    assert answer.value == 7
    assert answer.values == {"a": 1}


def test_final_round_withholds_tools_to_force_answer() -> None:
    # First round: tool_use. Second round (== max_rounds=1) must withhold tools so the
    # model answers instead of querying again.
    scripted = [
        _response([_tool_use_block("t1", "run_sql", {"query": "SELECT 1"})], "tool_use"),
        _response([_text_block('{"answer": "done", "value": 1, "values": {}}')], "end_turn"),
    ]
    client = StubClient(scripted)
    agent = SnowflakeAgent(tools=[StubRunSql()], config=_config(max_rounds=1), client=client)

    answer = agent.ask("q")

    assert answer.value == 1
    assert "tools" in client.messages.calls[0]
    assert "tools" not in client.messages.calls[1]


def test_round_cap_returns_graceful_answer_when_model_never_answers() -> None:
    # Empty script -> StubMessages keeps returning tool_use forever; the model never
    # emits final JSON. On the tool-withheld final round the loop still terminates
    # with a best-effort answer (value=None) instead of raising.
    client = StubClient([])
    agent = SnowflakeAgent(tools=[StubRunSql()], config=_config(max_rounds=3), client=client)

    answer = agent.ask("q")

    assert answer.value is None
    # max_rounds tool rounds + 1 forced-answer round = 4 model calls.
    assert len(client.messages.calls) == 4


def test_prose_without_json_is_nudged_back_onto_protocol() -> None:
    # Round 0: the model narrates ("Let me fetch it:") with no tool call and no JSON.
    # The loop should nudge it and continue rather than failing; round 1 then answers.
    scripted = [
        _response([_text_block("Sure — let me fetch that for you:")], "end_turn"),
        _response([_text_block('{"answer": "done", "value": 5, "values": {}}')], "end_turn"),
    ]
    client = StubClient(scripted)
    agent = SnowflakeAgent(tools=[StubRunSql()], config=_config(max_rounds=3), client=client)

    answer = agent.ask("q")

    assert answer.value == 5
    assert len(client.messages.calls) == 2


def test_unknown_tool_returns_error_to_model() -> None:
    scripted = [
        _response([_tool_use_block("t1", "nonexistent", {"x": 1})], "tool_use"),
        _response([_text_block('{"answer": "could not", "value": null, "values": {}}')], "end_turn"),
    ]
    client = StubClient(scripted)
    agent = SnowflakeAgent(tools=[StubRunSql()], config=_config(), client=client)

    answer = agent.ask("q")

    assert answer.value is None
    # the tool_result fed back on the 2nd call must be flagged as an error
    second_call_messages = client.messages.calls[1]["messages"]
    tool_result = second_call_messages[-1]["content"][0]
    assert tool_result["is_error"] is True
