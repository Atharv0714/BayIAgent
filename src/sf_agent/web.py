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

import anthropic
from fastapi import FastAPI, File, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, ValidationError

from sf_agent.agent import AgentError, SnowflakeAgent
from sf_agent.config import AgentConfig, CortexConfig, SnowflakeConfig, SnowflakeIngestConfig
from sf_agent.connection import SnowflakeConnection
from sf_agent import ingest_drafts
from sf_agent.ingest import IngestError, StructuredResult, structure_upload
from sf_agent.ingest_store import ensure_tables, write as ingest_write
from sf_agent.tools.cortex_analyst import CortexAnalystTool
from sf_agent.tools.run_sql import RunSqlTool

logger = logging.getLogger("sf_agent.web")

_STATIC = Path(__file__).parent / "static"
# Un-confirmed previews are persisted here so a user can leave and resume them; the
# dir sits at the project root (outside the package) and survives server restarts.
_DRAFTS_DIR = Path(__file__).resolve().parents[2] / ".ingest_drafts"

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
    # Write-capable connection for the ingest load path (separate creds/role); None
    # when SNOWFLAKE_INGEST_* isn't configured, in which case commit is unavailable.
    ingest_connection: SnowflakeConnection | None = None
    ingest_error: str | None = None
    # Shared Anthropic client for the one-shot structuring call.
    anthropic_client: anthropic.Anthropic | None = None
    agent_config: AgentConfig | None = None
    # ingest_id -> server-held validated payload, awaiting a commit. The commit writes
    # THIS (never a client re-submission), so the confirmation gate is real.
    pending_ingests: dict[str, StructuredResult] = {}


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

    # Shared Anthropic client + config for the one-shot ingest structuring call.
    STATE.agent_config = agent_config
    STATE.anthropic_client = anthropic.Anthropic(api_key=agent_config.api_key)

    # Ingest write connection is optional and kept strictly separate from the read
    # connection above, so the query path can stay read-only. Only configured when
    # SNOWFLAKE_INGEST_* is present; otherwise commit returns a clear "unavailable".
    try:
        ingest_config = SnowflakeIngestConfig()  # type: ignore[call-arg]
        STATE.ingest_connection = SnowflakeConnection(ingest_config).connect()
        logger.info("web: ingest write connection opened (role=%s)", ingest_config.role)
    except ValidationError:
        STATE.ingest_error = "SNOWFLAKE_INGEST_ROLE / _DATABASE / _SCHEMA not configured"
        logger.info("web: ingest commit unavailable (no SNOWFLAKE_INGEST_* creds)")

    # Rehydrate any drafts left un-confirmed in a previous run so they remain
    # committable (the in-memory cache is the commit source of truth).
    STATE.pending_ingests.update(ingest_drafts.load_all(_DRAFTS_DIR))
    if STATE.pending_ingests:
        logger.info("web: loaded %d ingest draft(s) from disk", len(STATE.pending_ingests))

    try:
        yield
    finally:
        if STATE.connection is not None:
            STATE.connection.close()
            logger.info("web: snowflake connection closed")
        if STATE.ingest_connection is not None:
            STATE.ingest_connection.close()
            logger.info("web: ingest write connection closed")


app = FastAPI(title="BayOne Snowflake Query Agent", lifespan=lifespan)


class AskRequest(BaseModel):
    question: str
    tool: str = "run_sql"
    session_id: str | None = None


@app.get("/")
def index() -> FileResponse:
    return FileResponse(_STATIC / "index.html")


@app.get("/ingest")
def ingest_page() -> FileResponse:
    return FileResponse(_STATIC / "ingest.html")


class CommitRequest(BaseModel):
    ingest_id: str


@app.post("/api/ingest/structure")
async def ingest_structure(file: UploadFile = File(...)) -> JSONResponse:
    """Structure one uploaded file into the two-table preview. Writes NOTHING.

    Returns the validated manifest/blocks/facts plus an ingest_id the client passes
    back to /api/ingest/commit to actually load the server-held payload.
    """
    if STATE.anthropic_client is None or STATE.agent_config is None:
        return JSONResponse({"ok": False, "error": "Structuring is not available."}, status_code=503)

    filename = file.filename or "upload"
    raw = await file.read()
    if not raw:
        return JSONResponse({"ok": False, "error": "The uploaded file is empty."}, status_code=400)

    try:
        with _LOCK:
            structured = structure_upload(
                STATE.anthropic_client, STATE.agent_config, filename, raw
            )
    except IngestError as e:
        # Unsupported type -> 415; other ingest failures (truncation, bad JSON) -> 422.
        status = 415 if "Unsupported file type" in str(e) else 422
        return JSONResponse({"ok": False, "error": str(e)}, status_code=status)
    except Exception as e:  # noqa: BLE001 — report to the browser rather than 500-ing opaquely
        logger.exception("web: ingest structuring failed for %r", filename)
        return JSONResponse({"ok": False, "error": f"Unexpected error: {e}"}, status_code=500)

    ingest_id = uuid.uuid4().hex
    with _LOCK:
        STATE.pending_ingests[ingest_id] = structured
        # Persist as a draft immediately, so leaving without confirming keeps the work.
        created_at = ingest_drafts.save(_DRAFTS_DIR, ingest_id, filename, structured)

    return JSONResponse(
        {
            "ok": True,
            "ingest_id": ingest_id,
            "source_file": filename,
            "created_at": created_at,
            "manifest": structured.manifest,
            "blocks": structured.blocks,
            "facts": structured.facts,
            "warnings": structured.warnings,
            "errors": structured.errors,
            "committable": structured.committable and STATE.ingest_connection is not None,
            "commit_available": STATE.ingest_connection is not None,
            "commit_reason": STATE.ingest_error,
            "elapsed_ms": structured.elapsed_ms,
            "tokens": structured.tokens,
            "cost_usd": structured.cost_usd,
        }
    )


@app.get("/api/ingest/drafts")
def ingest_drafts_list() -> JSONResponse:
    """Summaries of every record (drafts + committed) for the ingest history sidebar."""
    return JSONResponse(
        {
            "ok": True,
            "drafts": ingest_drafts.summaries(_DRAFTS_DIR),
            "commit_available": STATE.ingest_connection is not None,
            "commit_reason": STATE.ingest_error,
        }
    )


@app.get("/api/ingest/drafts/{ingest_id}")
def ingest_draft_get(ingest_id: str) -> JSONResponse:
    """Full stored record, shaped like /structure so the UI can re-render its preview."""
    record = ingest_drafts.get(_DRAFTS_DIR, ingest_id)
    if record is None:
        return JSONResponse({"ok": False, "error": "Record not found."}, status_code=404)
    result = StructuredResult(**record.get("result", {}))
    status = record.get("status", "draft")
    # Only un-committed drafts can still be loaded; committed records are read-only history.
    committable = (
        status == "draft" and result.committable and STATE.ingest_connection is not None
    )
    return JSONResponse(
        {
            "ok": True,
            "ingest_id": ingest_id,
            "source_file": record.get("source_file"),
            "created_at": record.get("created_at"),
            "status": status,
            "committed_at": record.get("committed_at"),
            "blocks_written": record.get("blocks_written"),
            "facts_written": record.get("facts_written"),
            "manifest": result.manifest,
            "blocks": result.blocks,
            "facts": result.facts,
            "warnings": result.warnings,
            "errors": result.errors,
            "committable": committable,
            "commit_available": STATE.ingest_connection is not None,
            "commit_reason": STATE.ingest_error,
            "elapsed_ms": result.elapsed_ms,
            "tokens": result.tokens,
            "cost_usd": result.cost_usd,
        }
    )


@app.delete("/api/ingest/drafts/{ingest_id}")
def ingest_draft_delete(ingest_id: str) -> JSONResponse:
    """Delete a history record (removes the file and any in-memory pending payload)."""
    with _LOCK:
        STATE.pending_ingests.pop(ingest_id, None)
        existed = ingest_drafts.delete(_DRAFTS_DIR, ingest_id)
    return JSONResponse({"ok": True, "deleted": existed})


@app.post("/api/ingest/commit")
def ingest_commit(req: CommitRequest) -> JSONResponse:
    """Load a previously structured, server-held payload into Snowflake."""
    if STATE.ingest_connection is None:
        reason = STATE.ingest_error or "ingest connection not configured"
        return JSONResponse({"ok": False, "error": f"Commit unavailable: {reason}"}, status_code=503)

    with _LOCK:
        structured = STATE.pending_ingests.get(req.ingest_id)
    if structured is None:
        return JSONResponse(
            {"ok": False, "error": "No pending upload for that id — re-structure the file."},
            status_code=400,
        )
    if not structured.committable:
        return JSONResponse(
            {"ok": False, "error": "This upload failed validation and cannot be ingested.", "errors": structured.errors},
            status_code=422,
        )

    try:
        with _LOCK:
            ensure_tables(STATE.ingest_connection)
            blocks_written, facts_written = ingest_write(
                STATE.ingest_connection, structured, req.ingest_id
            )
            STATE.pending_ingests.pop(req.ingest_id, None)
            ingest_drafts.mark_committed(
                _DRAFTS_DIR, req.ingest_id, blocks_written, facts_written
            )
    except Exception as e:  # noqa: BLE001 — surface the load failure to the browser
        logger.exception("web: ingest commit failed for id=%s", req.ingest_id)
        return JSONResponse({"ok": False, "error": f"Load failed: {e}"}, status_code=500)

    return JSONResponse(
        {
            "ok": True,
            "ingest_id": req.ingest_id,
            "blocks_written": blocks_written,
            "facts_written": facts_written,
        }
    )


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
