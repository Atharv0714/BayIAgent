from __future__ import annotations

import logging
import time
from typing import Any

from sf_agent.connection import SnowflakeConnection
from sf_agent.sql_guard import SqlGuardError, assert_read_only
from sf_agent.types import QueryResult, ToolError, ToolResult

logger = logging.getLogger("sf_agent.run_sql")


class RunSqlTool:
    """Runs a read-only SQL query against Snowflake and returns the rows.

    Tool boundary for untrusted (model-written) SQL: guards for read-only before
    executing, and records the executed SQL + elapsed_ms on every ToolResult so the
    agent loop can trace which query produced each answer.
    """

    name = "run_sql"
    description = (
        "Run a single read-only SQL SELECT query against the Snowflake warehouse and "
        "return the resulting rows. Only SELECT/WITH queries are permitted. Results "
        "are capped; check the `truncated` flag. Use this to ground every numeric "
        "answer in real data."
    )
    input_schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "A single read-only SQL SELECT (or WITH ... SELECT) statement.",
            }
        },
        "required": ["query"],
    }

    def __init__(self, connection: SnowflakeConnection) -> None:
        self._connection = connection

    def run(self, query: str, max_rows: int | None = None) -> ToolResult:  # type: ignore[override]
        start = time.perf_counter()
        try:
            assert_read_only(query)
            result: QueryResult = self._connection.execute(query, max_rows=max_rows)
            elapsed_ms = (time.perf_counter() - start) * 1000
            logger.info(
                "run_sql ok rows=%d truncated=%s elapsed_ms=%.1f sql=%s",
                result.row_count, result.truncated, elapsed_ms, query,
            )
            return ToolResult(ok=True, executed_sql=query, elapsed_ms=elapsed_ms, result=result)
        except SqlGuardError as e:
            elapsed_ms = (time.perf_counter() - start) * 1000
            logger.warning("run_sql guard_rejected elapsed_ms=%.1f sql=%s reason=%s", elapsed_ms, query, e)
            return ToolResult(
                ok=False, executed_sql=query, elapsed_ms=elapsed_ms,
                error=ToolError(type="guard_rejected", message=str(e)),
            )
        except Exception as e:  # noqa: BLE001 — surface any driver/SQL error to the loop as a typed result
            elapsed_ms = (time.perf_counter() - start) * 1000
            logger.warning("run_sql snowflake_error elapsed_ms=%.1f sql=%s error=%s", elapsed_ms, query, e)
            return ToolResult(
                ok=False, executed_sql=query, elapsed_ms=elapsed_ms,
                error=ToolError(type="snowflake_error", message=str(e)),
            )
