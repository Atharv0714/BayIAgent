"""Offline unit tests for CortexAnalystTool — no live Snowflake, no live REST.

These exercise the tool's own logic (SQL extraction from the Analyst response, the
read-only guard, the ToolResult shape) by injecting a stub Transport and a stub
connection. The live done-condition lives in test_cortex_evals.py; this file proves
the machinery around the network call without needing creds.
"""

from typing import Any

from sf_agent.config import CortexConfig
from sf_agent.tools.cortex_analyst import CortexAnalystTool
from sf_agent.types import QueryResult


def _config() -> CortexConfig:
    # Init kwargs outrank env/.env, so this never touches a real PAT.
    return CortexConfig(
        account="ORWKJXT-ZU20666",
        pat="fake-pat",
        semantic_view="DB.PUBLIC.BILLABLE_SEMANTIC",
    )


class _StubConnection:
    """Records the SQL it was asked to run and returns a canned QueryResult."""

    def __init__(self, result: QueryResult | None = None, raises: Exception | None = None) -> None:
        self.executed: list[str] = []
        self._result = result or QueryResult(
            columns=["N"], rows=[[617]], row_count=1, truncated=False
        )
        self._raises = raises

    def execute(self, sql: str, max_rows: int | None = None) -> QueryResult:
        self.executed.append(sql)
        if self._raises is not None:
            raise self._raises
        return self._result


def _transport_returning(response: dict[str, Any]):
    calls: list[dict[str, Any]] = []

    def transport(url: str, headers: dict[str, str], body: dict[str, Any], timeout_s: float) -> dict[str, Any]:
        calls.append({"url": url, "headers": headers, "body": body, "timeout_s": timeout_s})
        return response

    transport.calls = calls  # type: ignore[attr-defined]
    return transport


def test_extracts_sql_and_runs_it_through_the_connection() -> None:
    sql = "SELECT COUNT(*) AS N FROM DB.PUBLIC.BILLABLE"
    transport = _transport_returning(
        {"message": {"content": [{"type": "sql", "statement": sql}]}}
    )
    conn = _StubConnection()
    tool = CortexAnalystTool(conn, _config(), transport=transport)  # type: ignore[arg-type]

    result = tool.run(question="How many placement rows are there?")

    assert result.ok is True
    assert result.executed_sql == sql
    assert result.result is not None and result.result.rows == [[617]]
    assert result.error is None
    assert result.elapsed_ms >= 0
    # The generated SQL was actually executed against the connection.
    assert conn.executed == [sql]


def test_request_is_addressed_and_authenticated_correctly() -> None:
    transport = _transport_returning(
        {"message": {"content": [{"type": "sql", "statement": "SELECT 1"}]}}
    )
    cfg = _config()
    tool = CortexAnalystTool(_StubConnection(), cfg, transport=transport)  # type: ignore[arg-type]

    tool.run(question="anything")

    call = transport.calls[0]  # type: ignore[attr-defined]
    assert call["url"] == cfg.message_url
    assert call["headers"]["Authorization"] == "Bearer fake-pat"
    assert call["headers"]["X-Snowflake-Authorization-Token-Type"] == "PROGRAMMATIC_ACCESS_TOKEN"
    assert call["body"]["semantic_view"] == cfg.semantic_view
    assert call["body"]["messages"][0]["content"][0]["text"] == "anything"


def test_no_sql_in_response_is_a_typed_error() -> None:
    transport = _transport_returning(
        {"message": {"content": [{"type": "text", "text": "I need more detail."}]}}
    )
    conn = _StubConnection()
    tool = CortexAnalystTool(conn, _config(), transport=transport)  # type: ignore[arg-type]

    result = tool.run(question="vague")

    assert result.ok is False
    assert result.error is not None and result.error.type == "cortex_no_sql"
    assert "more detail" in result.error.message
    assert conn.executed == []  # nothing ran against the warehouse


def test_transport_failure_is_a_typed_error() -> None:
    def boom(url, headers, body, timeout_s):
        raise RuntimeError("cortex analyst HTTP 401: bad token")

    tool = CortexAnalystTool(_StubConnection(), _config(), transport=boom)  # type: ignore[arg-type]

    result = tool.run(question="anything")

    assert result.ok is False
    assert result.error is not None and result.error.type == "cortex_rest_error"
    assert "401" in result.error.message


def test_generated_sql_still_passes_through_the_readonly_guard() -> None:
    # Even though Analyst is trusted-ish, a mutating statement must be rejected
    # before it reaches the connection.
    transport = _transport_returning(
        {"message": {"content": [{"type": "sql", "statement": "DROP TABLE DB.PUBLIC.BILLABLE"}]}}
    )
    conn = _StubConnection()
    tool = CortexAnalystTool(conn, _config(), transport=transport)  # type: ignore[arg-type]

    result = tool.run(question="delete everything")

    assert result.ok is False
    assert result.error is not None and result.error.type == "guard_rejected"
    assert result.executed_sql == "DROP TABLE DB.PUBLIC.BILLABLE"
    assert conn.executed == []  # guard blocked it before execution
