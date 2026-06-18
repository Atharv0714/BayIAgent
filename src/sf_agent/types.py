import json
from typing import Any

from pydantic import BaseModel


class QueryResult(BaseModel):
    """Rows returned from a successful query, capped at the configured row limit."""

    columns: list[str]
    rows: list[list[Any]]
    row_count: int
    truncated: bool


class ToolError(BaseModel):
    type: str  # "guard_rejected" | "snowflake_error" | ...
    message: str


class ToolResult(BaseModel):
    """Uniform result envelope every tool returns.

    Keeping the shape identical across tools is what lets the chunk-2 agent loop
    swap run_sql for a Cortex Analyst tool without changing its dispatch code.
    executed_sql + elapsed_ms are always populated so the loop can trace which
    query produced each answer.
    """

    ok: bool
    executed_sql: str | None = None
    elapsed_ms: float = 0.0
    result: QueryResult | None = None
    error: ToolError | None = None

    def to_model_text(self) -> str:
        """Compact JSON the model can read and cite in a tool_result block."""
        if self.ok and self.result is not None:
            payload: dict[str, Any] = {
                "columns": self.result.columns,
                "rows": self.result.rows,
                "row_count": self.result.row_count,
                "truncated": self.result.truncated,
            }
        else:
            err = self.error
            payload = {"error": err.type if err else "unknown", "message": err.message if err else ""}
        return json.dumps(payload, default=str)
