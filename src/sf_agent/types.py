import json
from typing import Any

from pydantic import BaseModel, Field


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


class AgentAnswer(BaseModel):
    """Structured, auditable result of one agent run.

    `value` is the headline figure the question asked for (what the eval harness
    asserts on); `executed_sql` is every query the loop ran, in order, so an answer
    can be traced back to the data that produced it.
    """

    answer: str
    value: Any = None
    values: dict[str, Any] = Field(default_factory=dict)
    executed_sql: list[str] = Field(default_factory=list)
    # Optional visualization spec grounded in the pulled rows. None when the data
    # doesn't lend itself to a chart (e.g. a single scalar or a plain lookup). Shape
    # is a normalized {type, title, labels, series, ...} the web UI maps to Chart.js.
    chart: dict[str, Any] | None = None

    # Run diagnostics, surfaced in the UI's per-answer diagnostics panel.
    elapsed_ms: float = 0.0  # wall-clock time to produce this answer
    tokens: dict[str, int] = Field(default_factory=dict)  # input/output/cache_*/total
    cost_usd: float = 0.0  # estimated model cost for this answer
    sources: list[str] = Field(default_factory=list)  # data tables the SQL drew from
