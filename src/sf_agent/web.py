"""Minimal web UI over the SnowflakeAgent.

A single static page (static/index.html) posts a natural-language question plus a
tool choice to /api/ask; the backend runs the same agent loop used by the evals and
returns the grounded answer together with the exact SQL that produced it. Nothing
here changes the agent — it's a thin HTTP shell so a human can drive it.

Run it (from the project root, in a shell where .env loads):

    .venv/bin/python -m sf_agent.web

then open http://127.0.0.1:8000.
"""

from __future__ import annotations

import logging
import threading
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, ValidationError

from sf_agent.agent import AgentError, SnowflakeAgent
from sf_agent.config import AgentConfig, CortexConfig, SnowflakeConfig
from sf_agent.connection import SnowflakeConnection
from sf_agent.tools.cortex_analyst import CortexAnalystTool
from sf_agent.tools.run_sql import RunSqlTool

logger = logging.getLogger("sf_agent.web")

_STATIC = Path(__file__).parent / "static"

# The snowflake-connector connection is shared across requests; serialize agent
# runs with a lock so concurrent browser clicks don't interleave on one connection.
_LOCK = threading.Lock()


class _State:
    connection: SnowflakeConnection | None = None
    agents: dict[str, SnowflakeAgent] = {}
    errors: dict[str, str] = {}  # tool -> why it's unavailable
    # session_id -> running message history (SDK content blocks), kept server-side
    # so follow-ups can analyze data already fetched in earlier turns.
    conversations: dict[str, list[Any]] = {}


STATE = _State()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Open one Snowflake connection and build whichever agents are configured."""
    try:
        sf_config = SnowflakeConfig()  # type: ignore[call-arg]
    except ValidationError as e:
        # Fatal: no DB config means nothing to query. Surface it clearly.
        raise RuntimeError(
            f"Snowflake config missing ({e.error_count()} fields). Fill .env before starting."
        ) from e

    STATE.connection = SnowflakeConnection(sf_config).connect()
    logger.info("web: snowflake connection opened")

    try:
        agent_config = AgentConfig()  # type: ignore[call-arg]
    except ValidationError as e:
        STATE.connection.close()
        raise RuntimeError(f"ANTHROPIC_API_KEY missing; agent cannot start ({e}).") from e

    # run_sql is always available once the connection is up.
    run_sql_tool = RunSqlTool(STATE.connection)
    STATE.agents["run_sql"] = SnowflakeAgent(tools=[run_sql_tool], config=agent_config)

    # cortex_analyst is optional — only if a PAT + semantic view are configured.
    cortex_tool: CortexAnalystTool | None = None
    try:
        cortex_config = CortexConfig()  # type: ignore[call-arg]
        cortex_tool = CortexAnalystTool(STATE.connection, cortex_config)
        STATE.agents["cortex_analyst"] = SnowflakeAgent(tools=[cortex_tool], config=agent_config)
        logger.info("web: cortex_analyst agent ready")
    except ValidationError:
        STATE.errors["cortex_analyst"] = "SNOWFLAKE_PAT / CORTEX_SEMANTIC_VIEW not configured"
        logger.info("web: cortex_analyst unavailable (no PAT / semantic view)")

    # "auto" gives the agent both query tools so it can decide, per question, whether to
    # use semantic search (Cortex Analyst) or write raw SQL. Falls back to just run_sql
    # when Cortex isn't configured. (The database/followup/web route is chosen upstream
    # by the router for every mode; auto only picks *which query tool* to run.)
    auto_tools = [run_sql_tool] + ([cortex_tool] if cortex_tool is not None else [])
    STATE.agents["auto"] = SnowflakeAgent(tools=auto_tools, config=agent_config)
    logger.info("web: auto agent ready (%d query tools)", len(auto_tools))

    try:
        yield
    finally:
        if STATE.connection is not None:
            STATE.connection.close()
            logger.info("web: snowflake connection closed")


app = FastAPI(title="BayOne Snowflake Query Agent", lifespan=lifespan)


class AskRequest(BaseModel):
    question: str
    tool: str = "run_sql"
    session_id: str | None = None


@app.get("/")
def index() -> FileResponse:
    return FileResponse(_STATIC / "index.html")


@app.get("/api/tools")
def tools() -> dict[str, Any]:
    """Which tool paths this server can drive, so the UI can enable/disable them."""
    return {
        "tools": [
            {
                "id": "auto",
                "label": "Auto — agent picks SQL or semantic search",
                "available": "auto" in STATE.agents,
            },
            {"id": "run_sql", "label": "run_sql (Claude writes SQL)", "available": "run_sql" in STATE.agents},
            {
                "id": "cortex_analyst",
                "label": "Cortex Analyst (semantic search)",
                "available": "cortex_analyst" in STATE.agents,
                "reason": STATE.errors.get("cortex_analyst"),
            },
        ]
    }


@app.post("/api/ask")
def ask(req: AskRequest) -> JSONResponse:
    question = req.question.strip()
    if not question:
        return JSONResponse({"ok": False, "error": "Ask a question first."}, status_code=400)

    agent = STATE.agents.get(req.tool)
    if agent is None:
        reason = STATE.errors.get(req.tool, "unknown tool")
        return JSONResponse(
            {"ok": False, "error": f"Tool '{req.tool}' is unavailable: {reason}"}, status_code=400
        )

    # Continue an existing chat, or start a new one. History is kept server-side so a
    # follow-up ("now analyze that") sees the earlier turns and their fetched rows.
    session_id = req.session_id or uuid.uuid4().hex

    try:
        with _LOCK:
            history = STATE.conversations.get(session_id, [])
            # route_and_answer classifies (database / followup / web) first, then
            # dispatches — so the answer carries the route + reason for transparency.
            answer, updated = agent.route_and_answer(history, question)
            STATE.conversations[session_id] = updated
    except AgentError as e:
        logger.warning("web: agent could not answer q=%r err=%s", question, e)
        return JSONResponse({"ok": False, "error": f"The agent could not answer: {e}"}, status_code=502)
    except Exception as e:  # noqa: BLE001 — report any failure to the browser rather than 500-ing opaquely
        logger.exception("web: unexpected error answering q=%r", question)
        return JSONResponse({"ok": False, "error": f"Unexpected error: {e}"}, status_code=500)

    return JSONResponse(
        {
            "ok": True,
            "session_id": session_id,
            "tool": req.tool,
            "answer": answer.answer,
            "value": answer.value,
            "values": answer.values,
            "executed_sql": answer.executed_sql,
            "chart": answer.chart,
            "elapsed_ms": answer.elapsed_ms,
            "tokens": answer.tokens,
            "cost_usd": answer.cost_usd,
            "sources": answer.sources,
            "route": answer.route,
            "route_reason": answer.route_reason,
            "citations": answer.citations,
        }
    )


@app.post("/api/reset")
def reset(req: AskRequest) -> JSONResponse:
    """Drop a chat's server-side history so its memory is freed (New chat)."""
    if req.session_id:
        STATE.conversations.pop(req.session_id, None)
    return JSONResponse({"ok": True})


def main() -> None:
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8000, log_level="info")


if __name__ == "__main__":
    main()
