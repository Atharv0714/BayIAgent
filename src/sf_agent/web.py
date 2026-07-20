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
from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, ValidationError

from sf_agent.agent import AgentError, SnowflakeAgent, _extract_json
from sf_agent.config import (
    AgentConfig,
    AuthConfig,
    CortexConfig,
    SnowflakeConfig,
    SnowflakeIngestConfig,
)
from sf_agent.connection import SnowflakeConnection
from sf_agent import ingest_drafts
from sf_agent.ingest import (
    SENSITIVITIES,
    IngestError,
    StructuredResult,
    coerce_for_validation,
    stamp_sensitivity,
    structure_upload,
    tier_requirements,
    validate,
)
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
    # ingest_id -> {"sensitivity", "ingested_by"} stamped server-side at structure time
    # and applied at commit, so the owner tag can't be forged by the committing client.
    pending_meta: dict[str, dict[str, Any]] = {}
    # Per-owner enforcement config. Defaults keep today's behavior (no binding, all
    # rows shared) until the docs/sql migration is applied and ENFORCE_OWNERSHIP=true.
    auth_config: AuthConfig | None = None


STATE = _State()


def _caller_identity(request: Request) -> str | None:
    """Resolve the calling user's identity from the Azure Easy Auth header, falling
    back to the dev override when running locally. Returns None when neither is set."""
    cfg = STATE.auth_config
    if cfg is None:
        return None
    header = request.headers.get(cfg.easy_auth_header)
    if header and header.strip():
        return header.strip()
    return cfg.dev_caller_identity


def _caller_groups(request: Request) -> list[str]:
    """Resolve the caller's group memberships from the server-trusted groups header
    (SharePoint/M365 Entra claim), falling back to the dev list locally. The header is
    a comma-separated list; the browser can't set it (it's injected by the platform)."""
    cfg = STATE.auth_config
    if cfg is None:
        return []
    header = request.headers.get(cfg.groups_header)
    if header and header.strip():
        return [g.strip() for g in header.split(",") if g.strip()]
    return cfg.dev_group_list


def _caller_is_protected(request: Request) -> bool:
    """True when the caller belongs to the single privileged group that may read and
    author 'protected' data. Decided per request from the group claim — never row data."""
    cfg = STATE.auth_config
    if cfg is None:
        return False
    return cfg.protected_group in _caller_groups(request)


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

    # Per-owner enforcement config always loads (all fields have safe defaults, so it
    # never raises). With enforce_ownership false it's a no-op; true turns on identity
    # binding + the ingest-only private path.
    STATE.auth_config = AuthConfig()  # type: ignore[call-arg]
    if STATE.auth_config.enforce_ownership:
        logger.info(
            "web: per-owner enforcement ON (session var=%s)", STATE.auth_config.caller_session_var
        )

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


@app.get("/api/whoami")
def whoami(request: Request) -> JSONResponse:
    """Report the caller's resolved identity and whether they may author protected data,
    so the ingest page can show the Protected option only to privileged-group members.

    ``enforce`` mirrors the server flag: when false the page hides confidential tiers
    entirely (there's no binding, so marking data private/protected would be a no-op)."""
    auth = STATE.auth_config
    enforce = bool(auth and auth.enforce_ownership)
    return JSONResponse(
        {
            "ok": True,
            "enforce": enforce,
            "identity": _caller_identity(request),
            "is_protected_member": _caller_is_protected(request),
            "protected_group": auth.protected_group if auth else None,
        }
    )


class CommitRequest(BaseModel):
    ingest_id: str


class EditRequest(BaseModel):
    ingest_id: str
    blocks: list[dict[str, Any]] = []
    facts: list[dict[str, Any]] = []


class InstructRequest(BaseModel):
    ingest_id: str
    instruction: str


def _preview_payload(structured: StructuredResult, *, saved_draft: bool) -> dict[str, Any]:
    """The block of preview fields shared by /structure and /edit responses, so the UI
    re-renders an edited payload exactly like a freshly structured one."""
    return {
        "saved_draft": saved_draft,
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


@app.post("/api/ingest/structure")
async def ingest_structure(
    request: Request,
    file: UploadFile = File(...),
    save_draft: bool = Form(True),
    sensitivity: str = Form("internal"),
) -> JSONResponse:
    """Structure one uploaded file into the two-table preview.

    Returns the validated manifest/blocks/facts plus an ingest_id the client passes
    back to /api/ingest/commit to actually load the server-held payload.

    ``save_draft`` (default true) persists the structured payload to disk so leaving
    without confirming keeps the work and it shows in the history sidebar. Pass false
    for an *ingest-only* run: the payload is held in memory only and never touches the
    local disk, so after the commit (or on server restart) nothing remains here — the
    data lives solely in Snowflake.

    ``sensitivity`` is one of the three data tiers:
      * ``internal`` (default) — shared with everyone; today's behavior.
      * ``private`` — owned by the caller: rows are stamped ingested_by=<caller>, so the
        row-access policy shows them only to that person (plus a break-glass admin).
      * ``protected`` — group-scoped: readable only by the single privileged Entra group.
        Only a member of that group may select it (enforced here server-side).
    Both ``private`` and ``protected`` are confidential and therefore ingest-only, so
    they force ``save_draft`` off — their previews never touch this server's disk.
    """
    if STATE.anthropic_client is None or STATE.agent_config is None:
        return JSONResponse({"ok": False, "error": "Structuring is not available."}, status_code=503)

    if sensitivity not in ("internal", "private", "protected"):
        return JSONResponse(
            {"ok": False, "error": f"Unknown sensitivity {sensitivity!r}."}, status_code=400
        )

    # Resolve the ingester's identity SERVER-SIDE (Easy Auth header / dev override) —
    # never from the client — so a confidential row's owner can't be forged.
    ingested_by = _caller_identity(request)
    if sensitivity != "internal" and not ingested_by:
        return JSONResponse(
            {"ok": False, "error": "Cannot mark confidential without a signed-in identity."},
            status_code=401,
        )
    # Only members of the privileged group may author protected data — gate it here so a
    # non-member can't smuggle rows into the group-only tier by posting sensitivity.
    if sensitivity == "protected" and not _caller_is_protected(request):
        return JSONResponse(
            {"ok": False, "error": "You are not a member of the protected-data group."},
            status_code=403,
        )
    # Confidential data (private or protected) is ingest-only: keep nothing on disk here.
    if sensitivity != "internal":
        save_draft = False

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

    # Stamp the author's chosen tier onto every row, overwriting anything the model
    # emitted, so a row's sensitivity is always server-decided. The post-structure editor
    # refines individual rows later through the validated /edit gate.
    stamp_sensitivity(structured, sensitivity)

    ingest_id = uuid.uuid4().hex
    with _LOCK:
        STATE.pending_ingests[ingest_id] = structured
        # Remember the owner stamp server-side so commit applies it, not the client. The
        # per-row tier now lives on each block/fact; we only need the caller identity here
        # (stamped on confidential rows at write time; None keeps internal rows shared).
        STATE.pending_meta[ingest_id] = {
            "ingested_by": ingested_by if sensitivity != "internal" else None,
        }
        # Persist as a draft immediately so leaving without confirming keeps the work —
        # unless this is an ingest-only run, in which case nothing is written to disk.
        created_at = (
            ingest_drafts.save(_DRAFTS_DIR, ingest_id, filename, structured)
            if save_draft
            else None
        )

    return JSONResponse(
        {
            "ok": True,
            "ingest_id": ingest_id,
            "source_file": filename,
            "created_at": created_at,
            "saved_draft": save_draft,
            "sensitivity": sensitivity,
            "ingested_by": ingested_by if sensitivity != "internal" else None,
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


@app.post("/api/ingest/edit")
def ingest_edit(req: EditRequest, request: Request) -> JSONResponse:
    """Apply post-structuring edits (per-row tiers + manual field changes) to the
    server-held payload, then re-validate. The commit endpoint still writes THIS object,
    so the confirmation gate stays real — the client can't sneak data in at commit time.

    Per-row tiers are gated exactly like /structure: any private/protected row needs a
    resolved identity (401); any protected row needs privileged-group membership (403).
    """
    with _LOCK:
        current = STATE.pending_ingests.get(req.ingest_id)
    if current is None:
        return JSONResponse(
            {"ok": False, "error": "No pending upload for that id — re-structure the file."},
            status_code=400,
        )

    # Re-type manually edited cells (browser sends strings) so validation/loading match
    # the model path, then repair each row's tier to the closed set (default internal).
    blocks, facts = coerce_for_validation(req.blocks, req.facts)
    for rec in (*blocks, *facts):
        if rec.get("sensitivity") not in SENSITIVITIES:
            rec["sensitivity"] = "internal"

    tiers = [rec.get("sensitivity", "internal") for rec in (*blocks, *facts)]
    needs_identity, needs_group = tier_requirements(tiers)

    identity = _caller_identity(request)
    if needs_identity and not identity:
        return JSONResponse(
            {"ok": False, "error": "Cannot mark confidential without a signed-in identity."},
            status_code=401,
        )
    if needs_group and not _caller_is_protected(request):
        return JSONResponse(
            {"ok": False, "error": "You are not a member of the protected-data group."},
            status_code=403,
        )

    warnings, errors = validate(
        {"manifest": current.manifest, "blocks": blocks, "facts": facts}
    )
    edited = StructuredResult(
        manifest=current.manifest,
        blocks=blocks,
        facts=facts,
        warnings=warnings,
        errors=errors,
        elapsed_ms=current.elapsed_ms,
        tokens=current.tokens,
        cost_usd=current.cost_usd,
    )

    with _LOCK:
        STATE.pending_ingests[req.ingest_id] = edited
        STATE.pending_meta[req.ingest_id] = {
            "ingested_by": identity if needs_identity else None,
        }
        # Confidential edits are ingest-only: purge any on-disk draft so nothing
        # private/protected persists here. An all-internal edit keeps a draft (updating
        # it on disk when one already existed) so plain field fixes survive a restart.
        source_file = ingest_drafts.get(_DRAFTS_DIR, req.ingest_id)
        had_draft = source_file is not None
        if needs_identity:
            ingest_drafts.delete(_DRAFTS_DIR, req.ingest_id)
            saved_draft = False
        elif had_draft:
            ingest_drafts.save(
                _DRAFTS_DIR, req.ingest_id, source_file.get("source_file", "upload"), edited
            )
            saved_draft = True
        else:
            saved_draft = False

    return JSONResponse(
        {"ok": True, "ingest_id": req.ingest_id, **_preview_payload(edited, saved_draft=saved_draft)}
    )


def _row_catalog(blocks: list[dict[str, Any]], facts: list[dict[str, Any]]) -> str:
    """One line per row (with its stable key + a content snippet) so the model can decide
    which rows an instruction targets. Snippets are bounded to keep the call small."""
    lines = ["BLOCKS:"]
    for b in blocks:
        idx = b.get("block_index")
        if idx is None:
            continue
        theme = str(b.get("section_theme") or "").strip() or "-"
        title = str(b.get("section_title") or "").strip()
        text = str(b.get("text_content") or "").strip()[:200]
        lines.append(f"- block_index={idx} theme={theme!r} title={title!r} text={text!r} tier={b.get('sensitivity')}")
    lines.append("FACTS:")
    for f in facts:
        fid = f.get("fact_id")
        if fid is None:
            continue
        val = f.get("raw_value") or f.get("value_num") or f.get("value_text") or ""
        lines.append(
            f"- fact_id={fid!r} entity={str(f.get('entity_name') or '')!r} "
            f"attribute={str(f.get('attribute') or '')!r} value={str(val)!r} tier={f.get('sensitivity')}"
        )
    return "\n".join(lines)


def _apply_patches(
    rows: list[dict[str, Any]], patches: Any, key: str, is_member: bool
) -> int:
    """Apply the model's per-row patches onto `rows` in place, matched by `key`
    (block_index / fact_id). A `sensitivity` the caller can't author is downgraded
    (protected -> private for a non-member) or dropped if not a real tier. Other keys are
    applied verbatim — commit re-derives the security columns, so a stray field is inert.
    Returns how many rows changed."""
    if not isinstance(patches, list):
        return 0
    index = {str(r.get(key)): r for r in rows if r.get(key) is not None}
    changed = 0
    for patch in patches:
        if not isinstance(patch, dict) or patch.get(key) is None:
            continue
        target = index.get(str(patch[key]))
        if target is None:
            continue
        row_changed = False
        for field, val in patch.items():
            if field == key:
                continue
            if field == "sensitivity":
                if val not in SENSITIVITIES:
                    continue
                if val == "protected" and not is_member:
                    val = "private"
            target[field] = val
            row_changed = True
        changed += 1 if row_changed else 0
    return changed


@app.post("/api/ingest/instruct")
def ingest_instruct(req: InstructRequest, request: Request) -> JSONResponse:
    """Prompt-driven editor: apply the user's natural-language instruction to the
    structured rows (re-tiering and/or correcting field values) and hand the result back
    for review. Advisory only — nothing is persisted here; the user still clicks Save
    edits, which routes through /edit and re-gates every tier server-side. A tier the
    caller can't author is downgraded before it's returned."""
    instruction = req.instruction.strip()
    if not instruction:
        return JSONResponse({"ok": False, "error": "Enter an instruction first."}, status_code=400)
    if STATE.anthropic_client is None or STATE.agent_config is None:
        return JSONResponse({"ok": False, "error": "The editor model is not available."}, status_code=503)

    with _LOCK:
        current = STATE.pending_ingests.get(req.ingest_id)
    if current is None:
        return JSONResponse({"ok": False, "error": "No pending upload for that id."}, status_code=400)

    is_member = _caller_is_protected(request)
    # Work on copies; the real payload only changes when the user Saves (via /edit).
    blocks = [dict(b) for b in current.blocks]
    facts = [dict(f) for f in current.facts]

    allowed = "internal, private, or protected" if is_member else "internal or private"
    prompt = (
        "You are an editor for a consulting knowledge base. Apply the user's instruction to "
        "the structured rows below by changing each affected row's data-sensitivity tier "
        "and/or field values.\n"
        "Tiers: internal = shareable with everyone; private = personal/owner-restricted; "
        "protected = restricted to a privileged internal group (e.g. financials, margins, "
        "compensation, legal, M&A, board material).\n"
        f"Only assign tiers from: {allowed}.\n"
        'Return ONLY JSON of the form {"blocks":[{"block_index":<int>,"sensitivity":"<tier>"}],'
        '"facts":[{"fact_id":"<id>","sensitivity":"<tier>"}]}. Include only the rows you '
        "change and only the fields you change; to correct a value, include that field too.\n\n"
        f"USER INSTRUCTION:\n{instruction}\n\n"
        f"{_row_catalog(blocks, facts)}"
    )
    try:
        with _LOCK:
            resp = STATE.anthropic_client.messages.create(
                model=STATE.agent_config.model,
                max_tokens=4096,
                messages=[{"role": "user", "content": prompt}],
            )
    except Exception as e:  # noqa: BLE001 — advisory feature, report softly
        logger.warning("web: ingest instruct failed: %s", e)
        return JSONResponse({"ok": False, "error": f"Edit failed: {e}"}, status_code=502)

    text = "".join(getattr(b, "text", "") for b in resp.content if getattr(b, "type", None) == "text")
    try:
        raw = _extract_json(text)
    except Exception:  # noqa: BLE001
        raw = {}
    if not isinstance(raw, dict):
        raw = {}

    changed = _apply_patches(blocks, raw.get("blocks"), "block_index", is_member)
    changed += _apply_patches(facts, raw.get("facts"), "fact_id", is_member)

    note = None if is_member else "Protected is unavailable to you; any such rows became Private."
    return JSONResponse(
        {"ok": True, "blocks": blocks, "facts": facts, "changed": changed, "note": note}
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
        STATE.pending_meta.pop(ingest_id, None)
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

    # Guard: a confidential row must have an owner to stamp. pending_meta is in-memory
    # only, so a rehydrated draft has none — but confidential is ingest-only (never on
    # disk), so only all-internal drafts rehydrate. A missing owner here therefore means
    # the tier was set without a signed-in identity; refuse rather than write unowned.
    meta = STATE.pending_meta.get(req.ingest_id, {})
    has_confidential = any(
        rec.get("sensitivity") in ("private", "protected")
        for rec in (*structured.blocks, *structured.facts)
    )
    if has_confidential and not meta.get("ingested_by"):
        return JSONResponse(
            {"ok": False, "error": "Re-open and set the classification while signed in."},
            status_code=401,
        )

    try:
        with _LOCK:
            # write() reads each row's server-vetted tier and stamps the owner on
            # confidential rows (defaults keep internal rows shared and unowned).
            ensure_tables(STATE.ingest_connection)
            blocks_written, facts_written = ingest_write(
                STATE.ingest_connection,
                structured,
                req.ingest_id,
                ingested_by=meta.get("ingested_by"),
            )
            STATE.pending_ingests.pop(req.ingest_id, None)
            STATE.pending_meta.pop(req.ingest_id, None)
            # Ingest-only runs were never written to disk, so there's no draft file to
            # flip — mark_committed no-ops and returns False, leaving nothing behind.
            kept_history = ingest_drafts.mark_committed(
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
            "kept_history": kept_history,
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
def ask(req: AskRequest, request: Request) -> JSONResponse:
    question = req.question.strip()
    if not question:
        return JSONResponse({"ok": False, "error": "Ask a question first."}, status_code=400)

    agent = STATE.agents.get(req.tool)
    if agent is None:
        reason = STATE.errors.get(req.tool, "unknown tool")
        return JSONResponse(
            {"ok": False, "error": f"Tool '{req.tool}' is unavailable: {reason}"}, status_code=400
        )

    # When enforcement is on, resolve the caller's identity (Easy Auth header / dev
    # override) and require it — without one we can't scope private rows, so refuse
    # rather than silently show a broad view. Also resolve protected-group membership,
    # which decides whether this query may see the group-scoped 'protected' tier.
    auth = STATE.auth_config
    identity: str | None = None
    is_protected = False
    if auth is not None and auth.enforce_ownership:
        identity = _caller_identity(request)
        if not identity:
            return JSONResponse(
                {"ok": False, "error": "Authentication required."}, status_code=401
            )
        is_protected = _caller_is_protected(request)

    # Continue an existing chat, or start a new one. History is kept server-side so a
    # follow-up ("now analyze that") sees the earlier turns and their fetched rows.
    session_id = req.session_id or uuid.uuid4().hex

    try:
        with _LOCK:
            # Bind the caller identity + protected-group flag into the Snowflake session
            # variables the row-access policy reads, so this query returns internal rows,
            # this user's private rows, and (only for a group member) protected rows.
            # Safe on the shared read connection because _LOCK serializes every request.
            if identity is not None and STATE.connection is not None:
                STATE.connection.bind_session({  # type: ignore[union-attr]
                    auth.caller_session_var: identity,
                    auth.protected_session_var: "true" if is_protected else "false",
                })
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
