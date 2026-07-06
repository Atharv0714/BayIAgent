from __future__ import annotations

import logging
import time
from typing import Any, Callable

import requests

from sf_agent.config import CortexConfig
from sf_agent.connection import SnowflakeConnection
from sf_agent.sql_guard import SqlGuardError, assert_read_only
from sf_agent.types import QueryResult, ToolError, ToolResult

logger = logging.getLogger("sf_agent.cortex_analyst")

# A transport takes (url, headers, json_body, timeout_s) and returns the parsed
# JSON response dict. Injectable so tests can stub the HTTP round-trip.
Transport = Callable[[str, dict[str, str], dict[str, Any], float], dict[str, Any]]


def _requests_transport(
    url: str, headers: dict[str, str], body: dict[str, Any], timeout_s: float
) -> dict[str, Any]:
    resp = requests.post(url, headers=headers, json=body, timeout=timeout_s)
    if resp.status_code != 200:
        raise RuntimeError(f"cortex analyst HTTP {resp.status_code}: {resp.text[:500]}")
    return resp.json()


def _extract_sql(response: dict[str, Any]) -> str | None:
    """Pull the generated SQL statement out of an Analyst message response.

    SQL is returned in a content block of type 'sql' under the 'statement' key.
    """
    content = (response.get("message") or {}).get("content") or []
    for block in content:
        if block.get("type") == "sql" and block.get("statement"):
            return block["statement"]
    return None


def _extract_text(response: dict[str, Any]) -> str:
    """Concatenate any text/suggestion blocks — used to explain a no-SQL reply."""
    content = (response.get("message") or {}).get("content") or []
    parts: list[str] = []
    for block in content:
        if block.get("type") == "text" and block.get("text"):
            parts.append(block["text"])
        elif block.get("type") == "suggestions" and block.get("suggestions"):
            parts.append("suggestions: " + "; ".join(block["suggestions"]))
    return " ".join(parts) or "Cortex Analyst returned no SQL."


class CortexAnalystTool:
    """NL->SQL via Snowflake Cortex Analyst, executed against the warehouse.

    Conforms to the same Tool protocol as RunSqlTool. Cortex Analyst only *generates*
    SQL from a natural-language question (grounded in a semantic view); this tool then
    runs that SQL through the same guarded SnowflakeConnection, so the returned rows —
    and the value the agent reports — stay grounded in real data. The generated SQL is
    surfaced as `executed_sql` and the full round-trip is timed into `elapsed_ms`, so
    answers remain auditable exactly like run_sql.

    Because it satisfies the Tool protocol, the agent loop dispatches it with no change.
    """

    name = "cortex_analyst"
    description = (
        "Answer a natural-language question about the BayOne billable data by asking "
        "Snowflake Cortex Analyst, which translates the question into SQL over a curated "
        "semantic model and returns the resulting rows. Pass the user's question through "
        "as-is; do NOT write SQL yourself. Use this to ground every answer in real data."
    )
    input_schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "question": {
                "type": "string",
                "description": "A natural-language question about the data.",
            }
        },
        "required": ["question"],
    }

    def __init__(
        self,
        connection: SnowflakeConnection,
        config: CortexConfig,
        transport: Transport | None = None,
    ) -> None:
        self._connection = connection
        self._config = config
        self._transport = transport or _requests_transport

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._config.pat}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "X-Snowflake-Authorization-Token-Type": "PROGRAMMATIC_ACCESS_TOKEN",
        }

    def _body(self, question: str) -> dict[str, Any]:
        return {
            "semantic_view": self._config.semantic_view,
            "messages": [
                {"role": "user", "content": [{"type": "text", "text": question}]}
            ],
        }

    def run(self, question: str, max_rows: int | None = None) -> ToolResult:  # type: ignore[override]
        start = time.perf_counter()
        try:
            response = self._transport(
                self._config.message_url, self._headers(), self._body(question), self._config.timeout_s
            )
        except Exception as e:  # noqa: BLE001 — surface REST/transport failures as a typed result
            elapsed_ms = (time.perf_counter() - start) * 1000
            logger.warning("cortex_analyst rest_error elapsed_ms=%.1f q=%r error=%s", elapsed_ms, question, e)
            return ToolResult(
                ok=False, elapsed_ms=elapsed_ms,
                error=ToolError(type="cortex_rest_error", message=str(e)),
            )

        sql = _extract_sql(response)
        if not sql:
            elapsed_ms = (time.perf_counter() - start) * 1000
            msg = _extract_text(response)
            logger.warning("cortex_analyst no_sql elapsed_ms=%.1f q=%r msg=%s", elapsed_ms, question, msg)
            return ToolResult(
                ok=False, elapsed_ms=elapsed_ms,
                error=ToolError(type="cortex_no_sql", message=msg),
            )

        # Analyst-generated SQL is still run through the read-only guard before execution.
        try:
            assert_read_only(sql)
            result: QueryResult = self._connection.execute(sql, max_rows=max_rows)
        except SqlGuardError as e:
            elapsed_ms = (time.perf_counter() - start) * 1000
            logger.warning("cortex_analyst guard_rejected elapsed_ms=%.1f sql=%s reason=%s", elapsed_ms, sql, e)
            return ToolResult(
                ok=False, executed_sql=sql, elapsed_ms=elapsed_ms,
                error=ToolError(type="guard_rejected", message=str(e)),
            )
        except Exception as e:  # noqa: BLE001
            elapsed_ms = (time.perf_counter() - start) * 1000
            logger.warning("cortex_analyst snowflake_error elapsed_ms=%.1f sql=%s error=%s", elapsed_ms, sql, e)
            return ToolResult(
                ok=False, executed_sql=sql, elapsed_ms=elapsed_ms,
                error=ToolError(type="snowflake_error", message=str(e)),
            )

        elapsed_ms = (time.perf_counter() - start) * 1000
        logger.info(
            "cortex_analyst ok rows=%d elapsed_ms=%.1f sql=%s",
            result.row_count, elapsed_ms, sql,
        )
        return ToolResult(ok=True, executed_sql=sql, elapsed_ms=elapsed_ms, result=result)
