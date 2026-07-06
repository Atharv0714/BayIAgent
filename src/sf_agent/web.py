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
    STATE.agents["run_sql"] = SnowflakeAgent(
        tools=[RunSqlTool(STATE.connection)], config=agent_config
    )

    # cortex_analyst is optional — only if a PAT + semantic view are configured.
    try:
        cortex_config = CortexConfig()  # type: ignore[call-arg]
        STATE.agents["cortex_analyst"] = SnowflakeAgent(
            tools=[CortexAnalystTool(STATE.connection, cortex_config)], config=agent_config
        )
        logger.info("web: cortex_analyst agent ready")
    except ValidationError:
        STATE.errors["cortex_analyst"] = "SNOWFLAKE_PAT / CORTEX_SEMANTIC_VIEW not configured"
        logger.info("web: cortex_analyst unavailable (no PAT / semantic view)")

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


@app.get("/")
def index() -> FileResponse:
    return FileResponse(_STATIC / "index.html")


@app.get("/api/tools")
def tools() -> dict[str, Any]:
    """Which tool paths this server can drive, so the UI can enable/disable them."""
    return {
        "tools": [
            {"id": "run_sql", "label": "run_sql (Claude writes SQL)", "available": "run_sql" in STATE.agents},
            {
                "id": "cortex_analyst",
                "label": "Cortex Analyst (semantic view)",
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

    try:
        with _LOCK:
            answer = agent.ask(question)
    except AgentError as e:
        logger.warning("web: agent could not answer q=%r err=%s", question, e)
        return JSONResponse({"ok": False, "error": f"The agent could not answer: {e}"}, status_code=502)
    except Exception as e:  # noqa: BLE001 — report any failure to the browser rather than 500-ing opaquely
        logger.exception("web: unexpected error answering q=%r", question)
        return JSONResponse({"ok": False, "error": f"Unexpected error: {e}"}, status_code=500)

    return JSONResponse(
        {
            "ok": True,
            "tool": req.tool,
            "answer": answer.answer,
            "value": answer.value,
            "values": answer.values,
            "executed_sql": answer.executed_sql,
        }
    )


def main() -> None:
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8000, log_level="info")


if __name__ == "__main__":
    main()
