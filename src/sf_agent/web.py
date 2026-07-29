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

import base64
import binascii
import json
import logging
import os
import threading
import uuid
from collections import OrderedDict
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import anthropic
import anyio.to_thread
from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
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
    file_content_block,
    needs_vision,
    stamp_sensitivity,
    structure_upload,
    tier_requirements,
    validate,
)
from sf_agent.ingest_store import (
    category_for_tier,
    ensure_tables,
    read_registry,
    write as ingest_write,
)
from sf_agent.export import (
    FORMATS,
    answer_to_document,
    detect_export_request,
    render_document,
)
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
    # Separate client for document uploads (PDF/Word/PowerPoint/images) when a
    # vision-capable provider is configured; None means documents use the main client.
    vision_client: anthropic.Anthropic | None = None
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
    # upload_id -> record for a file attached in the chat. `kind` is "template" (a
    # .pptx/.potx carried to the renderer at export — record keeps "raw" bytes) or
    # "context" (a doc the model reads — record keeps the prebuilt "block", not raw).
    # In-memory, capped, per-process — like pending_ingests.
    uploads: "OrderedDict[str, dict[str, Any]]" = OrderedDict()


STATE = _State()

# Chat-attachment classification + retention. A PowerPoint upload is a DECK TEMPLATE
# (used by the renderer, never sent to the model); anything else is CONTEXT the model
# reads. Uploads are held in memory only, oldest evicted past these caps.
_TEMPLATE_EXTS = {".pptx", ".potx"}
_MAX_UPLOADS = 20
_MAX_UPLOAD_BYTES = 25 * 1024 * 1024  # 25 MB per file


def _remember_upload(upload_id: str, record: dict[str, Any]) -> None:
    """Store an upload record, evicting the oldest entries past the count cap (LRU-ish)."""
    STATE.uploads[upload_id] = record
    STATE.uploads.move_to_end(upload_id)
    while len(STATE.uploads) > _MAX_UPLOADS:
        STATE.uploads.popitem(last=False)


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


# App Service "Easy Auth" does NOT emit a simple comma-separated groups header. It
# injects X-MS-CLIENT-PRINCIPAL: base64 JSON holding every claim, with group object
# IDs as repeated "groups" claims. When GROUPS_HEADER names that header we decode it;
# any other header name keeps the original comma-separated contract (e.g. a
# SharePoint/M365 front end that maps the claim onto a plain header itself).
_PRINCIPAL_HEADER = "x-ms-client-principal"

# Entra omits the groups claim entirely once a user is in too many groups (the token
# would overflow), substituting these pointers to the Graph endpoint that lists them.
# We cannot resolve those without a Graph call, so their presence means "group
# membership is UNKNOWN" — which must read as "not a member", never as "no groups".
_OVERAGE_CLAIMS = {"_claim_names", "_claim_sources", "hasgroups"}


def _principal_groups(raw: str) -> list[str]:
    """Extract group claims from an Easy Auth X-MS-CLIENT-PRINCIPAL header value.

    Returns the group object IDs found in the claims array. Raises ValueError when the
    token hit the groups "overage" limit, so the caller can fail closed rather than
    silently treating an over-grouped user as belonging to nothing.
    """
    padded = raw.strip() + "=" * (-len(raw.strip()) % 4)
    try:
        payload = json.loads(base64.b64decode(padded).decode("utf-8"))
    except (binascii.Error, UnicodeDecodeError, json.JSONDecodeError) as e:
        raise ValueError(f"malformed {_PRINCIPAL_HEADER} header: {e}") from e

    claims = payload.get("claims") or []
    if not isinstance(claims, list):
        raise ValueError(f"malformed {_PRINCIPAL_HEADER} header: claims is not a list")

    groups: list[str] = []
    for claim in claims:
        if not isinstance(claim, dict):
            continue
        typ = str(claim.get("typ", ""))
        # Claim types arrive either bare ("groups") or as the full Microsoft URI.
        short = typ.rsplit("/", 1)[-1].lower()
        if short in _OVERAGE_CLAIMS:
            raise ValueError(
                "groups claim overflowed (overage); membership cannot be determined "
                "from the token alone and requires a Microsoft Graph lookup"
            )
        if short == "groups":
            val = str(claim.get("val", "")).strip()
            if val:
                groups.append(val)
    return groups


def _caller_groups(request: Request) -> list[str]:
    """Resolve the caller's group memberships from the server-trusted groups header,
    falling back to the dev list locally. The browser cannot set either header — both
    are injected by the platform and stripped from inbound requests.

    Two wire formats are supported, chosen by the configured header NAME:
      * X-MS-CLIENT-PRINCIPAL — Azure Easy Auth base64 JSON claims (group object IDs).
      * anything else — a plain comma-separated list of group names/ids.
    """
    cfg = STATE.auth_config
    if cfg is None:
        return []
    header = request.headers.get(cfg.groups_header)
    if header and header.strip():
        if cfg.groups_header.strip().lower() == _PRINCIPAL_HEADER:
            try:
                return _principal_groups(header)
            except ValueError as e:
                # Fail CLOSED: an unreadable claim must never widen access.
                logger.warning("could not read group claims (%s); treating caller as ungrouped", e)
                return []
        return [g.strip() for g in header.split(",") if g.strip()]
    return cfg.dev_group_list


def _caller_is_protected(request: Request) -> bool:
    """True when the caller belongs to the single privileged group that may read and
    author 'protected' data. Decided per request from the group claim — never row data."""
    cfg = STATE.auth_config
    if cfg is None:
        return False
    return cfg.protected_group in _caller_groups(request)


def _scope_read_internal_only() -> None:
    """Fail-closed scoping for the shared read connection before an UNBOUND read.

    The one read connection carries whatever ``BAYI_CALLER`` the previous ``/api/ask``
    bound. Once ``rap_ownership`` is attached, any query that does NOT set an identity
    first inherits that stale caller — so an unbound read (the vocabulary registry) would
    read through the *previous* user's private/protected rows. The registry is a shared
    vocabulary helper (distinct entity_type/attribute values fed back to structuring), so
    it must only ever see 'internal' rows. Binding the caller to unset + protected='false'
    makes the policy return internal rows only, regardless of who queried last. No-op when
    enforcement is off (nothing reads the variables) and safe to leave set between requests
    — /api/ask re-binds the real caller before it queries."""
    auth = STATE.auth_config
    if auth is None or STATE.connection is None:
        return
    STATE.connection.bind_session(
        {auth.caller_session_var: None, auth.protected_session_var: "false"}
    )


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

    # Some launch environments export ANTHROPIC_API_KEY as an empty string (e.g. a login
    # shell or parent process that declares the var without a value). pydantic-settings
    # gives real env vars priority over .env, so an empty export silently shadows the real
    # key in .env — the app then starts fine but every Claude call fails auth at request
    # time. Drop the empty shadow so .env stays authoritative for local runs.
    if os.environ.get("ANTHROPIC_API_KEY", "").strip() == "":
        os.environ.pop("ANTHROPIC_API_KEY", None)

    # The key and the endpoint must travel TOGETHER: a key from .env paired with a base URL
    # inherited from the shell means the key is sent to the wrong provider, which fails as an
    # opaque "401 invalid x-api-key" on the first call (not at startup). This bites whenever a
    # login shell exports ANTHROPIC_BASE_URL (e.g. pointing at Anthropic) while .env selects a
    # different provider such as Z.AI. When the environment supplies a base URL but NOT a key,
    # its base URL is stale — drop it so .env is authoritative for both halves of the pair.
    if os.environ.get("ANTHROPIC_BASE_URL", "").strip() and "ANTHROPIC_API_KEY" not in os.environ:
        stale = os.environ.pop("ANTHROPIC_BASE_URL")
        logger.warning(
            "web: ignoring ANTHROPIC_BASE_URL=%s from the environment (no matching "
            "ANTHROPIC_API_KEY there); using the provider configured in .env instead",
            stale,
        )

    try:
        agent_config = AgentConfig()  # type: ignore[call-arg]
    except ValidationError as e:
        STATE.connection.close()
        raise RuntimeError(f"ANTHROPIC_API_KEY missing; agent cannot start ({e}).") from e

    # Log the resolved provider so a misrouted key is obvious at startup, not at first call.
    logger.info(
        "web: LLM endpoint=%s model=%s",
        agent_config.base_url or "https://api.anthropic.com (default)",
        agent_config.model,
    )

    # run_sql is always available once the connection is up.
    run_sql_tool = RunSqlTool(STATE.connection)
    STATE.agents["run_sql"] = SnowflakeAgent(tools=[run_sql_tool], config=agent_config)

    # cortex_analyst is optional — only if a PAT + semantic view are configured AND
    # CORTEX_ENABLED isn't turned off. Disabled (or misconfigured) -> "auto" uses run_sql
    # only, so no Cortex Analyst per-message charges and no failed round-trips.
    cortex_tool: CortexAnalystTool | None = None
    try:
        cortex_config = CortexConfig()  # type: ignore[call-arg]
    except ValidationError:
        cortex_config = None
        STATE.errors["cortex_analyst"] = "SNOWFLAKE_PAT / CORTEX_SEMANTIC_VIEW not configured"
        logger.info("web: cortex_analyst unavailable (no PAT / semantic view)")

    if cortex_config is not None and not cortex_config.enabled:
        STATE.errors["cortex_analyst"] = "disabled (CORTEX_ENABLED=false)"
        logger.info("web: cortex_analyst disabled via CORTEX_ENABLED=false")
    elif cortex_config is not None:
        cortex_tool = CortexAnalystTool(STATE.connection, cortex_config)
        STATE.agents["cortex_analyst"] = SnowflakeAgent(tools=[cortex_tool], config=agent_config)
        logger.info("web: cortex_analyst agent ready")

    # "auto" gives the agent both query tools so it can decide, per question, whether to
    # use semantic search (Cortex Analyst) or write raw SQL. Falls back to just run_sql
    # when Cortex isn't configured. (The database/followup/web route is chosen upstream
    # by the router for every mode; auto only picks *which query tool* to run.)
    auto_tools = [run_sql_tool] + ([cortex_tool] if cortex_tool is not None else [])
    STATE.agents["auto"] = SnowflakeAgent(tools=auto_tools, config=agent_config)
    logger.info("web: auto agent ready (%d query tools)", len(auto_tools))

    # Shared Anthropic client + config for the one-shot ingest structuring call. An explicit
    # timeout matters here: the SDK's default is 600s, so a stalled provider connection would
    # hold the ingest slot for ten minutes and look like a hung upload. Structuring a large
    # document legitimately takes minutes, so this is generous but bounded.
    STATE.agent_config = agent_config
    STATE.anthropic_client = anthropic.Anthropic(
        api_key=agent_config.api_key,
        base_url=agent_config.base_url,
        timeout=300.0,
    )

    # Document-vision lane: PDFs and Office files need a provider that decodes `document`
    # content blocks. When configured, document uploads route here while every query (and
    # text/spreadsheet ingest) stays on the cheap main provider.
    if agent_config.vision_enabled:
        STATE.vision_client = anthropic.Anthropic(
            api_key=agent_config.vision_api_key,
            base_url=agent_config.vision_base_url,
            timeout=300.0,
        )
        logger.info(
            "web: document-vision lane ready endpoint=%s model=%s",
            agent_config.vision_base_url or "https://api.anthropic.com (default)",
            agent_config.vision_model,
        )
    elif agent_config.base_url:
        # Main provider is non-default (e.g. Z.AI) and no vision lane is set: PDF/Office
        # uploads will come back empty because the model can't read document blocks.
        logger.warning(
            "web: no INGEST_VISION_API_KEY set while the main provider is %s — PDF/Word/"
            "PowerPoint uploads may return no content; text and .xlsx uploads are fine",
            agent_config.base_url,
        )

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
    # A file attached to this turn (from /api/upload). A "context" upload's content is
    # read by the model; a "template" upload is carried through to the deck export.
    upload_id: str | None = None


@app.get("/")
def index() -> FileResponse:
    return FileResponse(_STATIC / "index.html")


@app.post("/api/upload")
async def upload(file: UploadFile = File(...)) -> JSONResponse:
    """Accept one file attached in the chat and hold it server-side for this session.

    Classifies it: a PowerPoint (.pptx/.potx) is a deck TEMPLATE (used by the renderer at
    export, never sent to the model); anything else is CONTEXT the model reads (validated
    here by trying to build its content block). Returns an upload_id the client passes to
    /api/ask (context) and, for a template, on to /api/export.
    """
    filename = file.filename or "upload"
    raw = await file.read()
    if not raw:
        return JSONResponse({"ok": False, "error": "The uploaded file is empty."}, status_code=400)
    if len(raw) > _MAX_UPLOAD_BYTES:
        return JSONResponse(
            {"ok": False, "error": f"File is too large (max {_MAX_UPLOAD_BYTES // (1024 * 1024)} MB)."},
            status_code=413,
        )

    ext = Path(filename).suffix.lower()
    if ext in _TEMPLATE_EXTS:
        record: dict[str, Any] = {"filename": filename, "kind": "template", "raw": raw}
    else:
        # Context: build the model content block now (raises for unsupported types) so the
        # user gets an immediate 415, and so an Office file is converted to PDF only once.
        try:
            block = file_content_block(filename, raw)
        except IngestError as e:
            status = 415 if "Unsupported file type" in str(e) else 422
            return JSONResponse({"ok": False, "error": str(e)}, status_code=status)
        except Exception as e:  # noqa: BLE001
            logger.exception("web: upload processing failed for %r", filename)
            return JSONResponse({"ok": False, "error": f"Could not read file: {e}"}, status_code=500)
        record = {"filename": filename, "kind": "context", "block": block}

    upload_id = uuid.uuid4().hex
    with _LOCK:
        _remember_upload(upload_id, record)
    return JSONResponse(
        {"ok": True, "upload_id": upload_id, "filename": filename, "kind": record["kind"]}
    )


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

    # Feed the model the vocabulary already in the warehouse so it reuses existing
    # entity_types/attributes instead of minting near-duplicates. Read under the shared
    # read lock (same connection the agent uses); best-effort — a failed read yields an
    # empty registry and structuring proceeds on the guide alone.
    # Everything below blocks: a Snowflake query and a multi-second (sometimes multi-minute)
    # LLM call. This is the ONLY `async def` handler, so running that work inline would block
    # the event loop and freeze the WHOLE server — every other request, including the ingest
    # page itself, hangs until structuring finishes. Offload it to a worker thread (the same
    # thing FastAPI does automatically for plain `def` handlers) so the app stays responsive.
    # `_LOCK` is still what serializes the shared Snowflake connection and the model client.
    def _structure_blocking() -> StructuredResult:
        registry: dict[str, list[str]] = {}
        if STATE.connection is not None:
            with _LOCK:
                # Scope to internal-only FIRST: this read binds no caller, so once the
                # row-access policy is live it would otherwise inherit whatever identity the
                # last /api/ask left on the shared session. The registry is shared vocabulary
                # and must never surface a private/protected row's entity_type/attribute.
                _scope_read_internal_only()
                registry = read_registry(STATE.connection)
        # Route document uploads (PDF/Office/image) to the vision provider when one is
        # configured; text and spreadsheets stay on the main provider. `model_copy` swaps
        # only the model so the rest of the agent config (token ceiling) is unchanged.
        client = STATE.anthropic_client
        cfg = STATE.agent_config
        if needs_vision(filename) and STATE.vision_client is not None:
            client = STATE.vision_client
            cfg = cfg.model_copy(update={"model": cfg.vision_model})
        with _LOCK:
            return structure_upload(client, cfg, filename, raw, registry=registry)

    # Fail loudly instead of silently returning an empty structure: without a vision lane a
    # non-Anthropic main provider cannot read document blocks at all.
    if (
        needs_vision(filename)
        and STATE.vision_client is None
        and STATE.agent_config.base_url
    ):
        return JSONResponse(
            {
                "ok": False,
                "error": (
                    f"This server's model provider cannot read '{filename}' (PDF, Word, "
                    "PowerPoint and images need document vision). Set INGEST_VISION_API_KEY "
                    "to route document uploads to a vision-capable provider, or upload a "
                    "CSV/TXT/MD/XLSX version."
                ),
            },
            status_code=422,
        )

    try:
        structured = await anyio.to_thread.run_sync(_structure_blocking)
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
) -> list[dict[str, Any]]:
    """Apply the model's per-row patches onto `rows` in place, matched by `key`
    (block_index / fact_id). A `sensitivity` the caller can't author is downgraded
    (protected -> private for a non-member) or dropped if not a real tier. `reason` is a
    model-supplied explanation surfaced to the user, never written onto the row. Other
    fields are applied verbatim — commit re-derives the security columns, so a stray field
    is inert.

    Returns one change-record per row actually changed, each carrying the row's key, a
    content snippet, the resulting tier + human-facing category, whether the tier moved,
    and the model's reason — so the UI can show, right under the edit, exactly which rows
    were reclassified and why."""
    if not isinstance(patches, list):
        return []
    index = {str(r.get(key)): r for r in rows if r.get(key) is not None}
    changes: list[dict[str, Any]] = []
    for patch in patches:
        if not isinstance(patch, dict) or patch.get(key) is None:
            continue
        target = index.get(str(patch[key]))
        if target is None:
            continue
        reason = str(patch.get("reason") or "").strip()
        before_tier = target.get("sensitivity")
        row_changed = False
        for field, val in patch.items():
            if field in (key, "reason"):
                continue
            if field == "sensitivity":
                if val not in SENSITIVITIES:
                    continue
                if val == "protected" and not is_member:
                    val = "private"
            target[field] = val
            row_changed = True
        if not row_changed:
            continue
        after_tier = target.get("sensitivity")
        snippet = (
            str(target.get("text_content") or "").strip()[:160]
            if key == "block_index"
            else _fact_snippet(target)
        )
        changes.append(
            {
                key: target.get(key),
                "snippet": snippet,
                "sensitivity": after_tier,
                "sensitivity_category": (
                    category_for_tier(after_tier) if after_tier in SENSITIVITIES else after_tier
                ),
                "tier_changed": before_tier != after_tier,
                "reason": reason,
            }
        )
    return changes


def _fact_snippet(fact: dict[str, Any]) -> str:
    """A short human label for a fact row in the changes list."""
    val = fact.get("raw_value") or fact.get("value_num") or fact.get("value_text") or ""
    entity = str(fact.get("entity_name") or "").strip()
    attribute = str(fact.get("attribute") or "").strip()
    label = " ".join(p for p in (entity, attribute) if p) or "fact"
    return f"{label}: {val}".strip()[:160]


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
        'Return ONLY JSON of the form {"blocks":[{"block_index":<int>,"sensitivity":"<tier>",'
        '"reason":"<why>"}],"facts":[{"fact_id":"<id>","sensitivity":"<tier>","reason":"<why>"}]}. '
        "Include only the rows you change and only the fields you change; to correct a value, "
        "include that field too. For EVERY changed row also include a short `reason` (one "
        "sentence) that quotes the specific line or phrase in that row that justifies the new "
        "tier, so the user can see exactly why you reclassified it.\n\n"
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

    block_changes = _apply_patches(blocks, raw.get("blocks"), "block_index", is_member)
    fact_changes = _apply_patches(facts, raw.get("facts"), "fact_id", is_member)
    changed = len(block_changes) + len(fact_changes)

    note = None if is_member else "Protected is unavailable to you; any such rows became Private."
    return JSONResponse(
        {
            "ok": True,
            "blocks": blocks,
            "facts": facts,
            "changed": changed,
            "changes": {"blocks": block_changes, "facts": fact_changes},
            "note": note,
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

    # Resolve an attached file. A "context" upload's prebuilt content block rides with the
    # question so the model reads it; a "template" upload is carried through to the deck
    # export (never sent to the model). Built outside _LOCK so any conversion doesn't hold
    # the shared read connection.
    attachments: list[dict[str, Any]] | None = None
    template_id: str | None = None
    if req.upload_id:
        up = STATE.uploads.get(req.upload_id)
        if up is None:
            return JSONResponse(
                {"ok": False, "error": "Attached file expired — re-attach it."}, status_code=400
            )
        if up.get("kind") == "template":
            template_id = req.upload_id
        elif up.get("block") is not None:
            attachments = [up["block"]]

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
            # route_and_answer classifies (database / followup / web / general) first, then
            # dispatches — so the answer carries the route + reason for transparency. An
            # attached context doc rides along as content blocks the model can read.
            answer, updated = agent.route_and_answer(history, question, attachments=attachments)
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
            # When the question asked for an office document ("make a PPTX of…"), tell the
            # UI which format so it auto-renders the file from THIS grounded answer via
            # /api/export — no re-query, no extra model call. None for ordinary questions.
            "export_format": detect_export_request(question),
            # When a .pptx/.potx template was attached, echo its id so the UI passes it to
            # /api/export and the deck renders onto that template. None otherwise.
            "template_id": template_id,
        }
    )


class ExportRequest(BaseModel):
    # One of the SUPPORTED_FORMATS: xlsx | docx | pptx | pdf.
    format: str
    # The question that produced the answer — becomes the document title.
    question: str | None = None
    # The full /api/ask answer payload (value / answer / values / chart / sources). The
    # export is rendered from THIS grounded answer, so it never re-queries or re-invents
    # numbers; every figure in the document already came from a live query.
    answer: dict[str, Any] = {}
    # For a pptx: the upload_id of an attached .pptx/.potx template (from /api/upload) to
    # render the deck onto. Ignored for non-pptx formats and when the template has expired.
    template_id: str | None = None


def _export_slug(text: str) -> str:
    """A short, filesystem-safe slug from the question for the download filename."""
    keep = [c.lower() if c.isalnum() else "-" for c in (text or "")]
    slug = "".join(keep).strip("-")
    while "--" in slug:
        slug = slug.replace("--", "-")
    return (slug[:48].strip("-")) or "report"


@app.post("/api/export")
def export(req: ExportRequest) -> Response:
    """Render one grounded answer into a downloadable native document.

    The client posts the answer it already holds (no new model call, no re-query) plus a
    target format; the server maps it to a format-agnostic document model and streams back
    the file with a download filename derived from the question.
    """
    fmt = (req.format or "").lower().strip()
    if fmt not in FORMATS:
        return JSONResponse(
            {"ok": False, "error": f"Unsupported format {req.format!r}. Choose one of: {', '.join(FORMATS)}."},
            status_code=400,
        )
    if not isinstance(req.answer, dict) or not req.answer:
        return JSONResponse({"ok": False, "error": "No answer to export."}, status_code=400)

    # A pptx may render onto a user-uploaded template (its theme + layouts). Look it up by
    # id; a missing/expired template just falls back to the built-in default deck.
    template: bytes | None = None
    if req.template_id and fmt == "pptx":
        up = STATE.uploads.get(req.template_id)
        if up is not None and up.get("kind") == "template":
            template = up.get("raw")

    try:
        model = answer_to_document(req.answer, req.question)
        data = render_document(model, fmt, template=template)
    except Exception as e:  # noqa: BLE001 — surface render failures to the browser
        logger.exception("web: export failed fmt=%s", fmt)
        return JSONResponse({"ok": False, "error": f"Could not generate {fmt}: {e}"}, status_code=500)

    ext, mime = FORMATS[fmt]
    filename = f"bayi-{_export_slug(req.question or '')}-{model.generated_on}.{ext}"
    return Response(
        content=data,
        media_type=mime,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
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
