"""The general-assistant lane: route 'general' answers from the model with NO query tools.

Offline — a stub client replays the router decision then the answer. Verifies dispatch,
that no SQL tool runs, that the general system prompt is used, and that decks still work.
"""

from types import SimpleNamespace
from typing import Any

from sf_agent.agent import SnowflakeAgent
from sf_agent.config import AgentConfig
from sf_agent.prompts import GENERAL_SYSTEM
from sf_agent.types import QueryResult, ToolResult


def _text(text: str) -> SimpleNamespace:
    return SimpleNamespace(type="text", text=text)


def _resp(content: list[SimpleNamespace], stop: str = "end_turn") -> SimpleNamespace:
    return SimpleNamespace(content=content, stop_reason=stop, usage=None)


class _StubMessages:
    def __init__(self, scripted: list[SimpleNamespace]) -> None:
        self._scripted = scripted
        self.calls: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> SimpleNamespace:
        self.calls.append({**kwargs, "messages": list(kwargs["messages"])})
        return self._scripted.pop(0)


class _StubClient:
    def __init__(self, scripted: list[SimpleNamespace]) -> None:
        self.messages = _StubMessages(scripted)


class _StubRunSql:
    name = "run_sql"
    description = "stub"
    input_schema = {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}

    def __init__(self) -> None:
        self.calls: list[str] = []

    def run(self, query: str, max_rows: int | None = None) -> ToolResult:
        self.calls.append(query)
        return ToolResult(
            ok=True, executed_sql=query, elapsed_ms=1.0,
            result=QueryResult(columns=["N"], rows=[[1]], row_count=1, truncated=False),
        )


def _cfg() -> AgentConfig:
    return AgentConfig(api_key="test-key", _env_file=None)  # type: ignore[call-arg]


def test_general_route_answers_without_tools() -> None:
    scripted = [
        _resp([_text('{"route":"general","reason":"casual request"}')]),  # router
        _resp([_text('{"answer":"Waves in blue.","value":null,"values":{}}')]),  # general answer
    ]
    client = _StubClient(scripted)
    tool = _StubRunSql()
    agent = SnowflakeAgent(tools=[tool], config=_cfg(), client=client)

    answer, _ = agent.route_and_answer([], "write a haiku about consulting")

    assert answer.route == "general"
    assert answer.answer == "Waves in blue."
    assert tool.calls == []  # no SQL tool ran
    # The answer call (2nd create) advertised NO tools and used the general system prompt.
    assert "tools" not in client.messages.calls[1]
    assert client.messages.calls[1]["system"][0]["text"] == GENERAL_SYSTEM


def test_general_route_supports_deck_values_slides() -> None:
    scripted = [
        _resp([_text('{"route":"general","reason":"draft a deck"}')]),
        _resp([_text('{"answer":"Deck ready.","value":null,'
                     '"values":{"slides":[{"title":"Agile 101","content":["iterate"]}]}}')]),
    ]
    client = _StubClient(scripted)
    agent = SnowflakeAgent(tools=[_StubRunSql()], config=_cfg(), client=client)

    answer, _ = agent.route_and_answer([], "make a deck explaining agile")

    assert answer.route == "general"
    assert answer.values["slides"][0]["title"] == "Agile 101"


def test_database_route_still_uses_tools() -> None:
    # Regression: a database route must still advertise tools on its query call.
    scripted = [
        _resp([_text('{"route":"database","reason":"needs internal data"}')]),
        _resp([SimpleNamespace(type="tool_use", id="t1", name="run_sql", input={"query": "SELECT 1"},
                               usage=None)], "tool_use"),
        _resp([_text('{"answer":"1 row.","value":1,"values":{}}')]),
    ]
    client = _StubClient(scripted)
    agent = SnowflakeAgent(tools=[_StubRunSql()], config=_cfg(), client=client)

    answer, _ = agent.route_and_answer([], "how many rows in facts?")

    assert answer.route == "database"
    assert answer.value == 1
    assert "tools" in client.messages.calls[1]  # the query call advertised tools
