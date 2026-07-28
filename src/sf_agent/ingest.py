"""Structuring step of the ingest page.

One uploaded file -> one Claude call -> the fixed two-table JSON contract
(`blocks` for retrieval, `facts` for analytics, plus a `manifest`) defined in
`ingest_prompt.md`. This module only *structures and validates*; the write to
Snowflake lives in `ingest_store.py` and runs only after the user confirms.

Native multimodal input only (no parsing deps): PDFs and images go to Claude as
`document` / `image` content blocks; text-family files as decoded text. Office
formats are converted to PDF on the server via LibreOffice (headless) and then take
the PDF path; if LibreOffice is absent they fall back to a convert-to-PDF message.
"""

from __future__ import annotations

import base64
import logging
import os
import shutil
import subprocess
import tempfile
import time
from collections.abc import Iterable
from datetime import date
from pathlib import Path
from typing import Any

import anthropic
from pydantic import BaseModel, Field

from sf_agent.agent import _estimate_cost, _extract_json
from sf_agent.config import AgentConfig
from sf_agent.router import usage_from

logger = logging.getLogger("sf_agent.ingest")

# The structuring guide, loaded once and marked for prompt caching so it's billed
# at the cache rate across uploads (mirrors the cached_system shape in agent.py).
_GUIDE = (Path(__file__).parent / "ingest_prompt.md").read_text("utf-8")
INGEST_SYSTEM = [{"type": "text", "text": _GUIDE, "cache_control": {"type": "ephemeral"}}]

# Extension -> how the bytes reach Claude. PDFs/images use native content blocks;
# text-family files are decoded inline. Office formats are converted to PDF first (see
# _office_to_pdf) and then follow the PDF path.
_PDF_EXTS = {".pdf"}
_IMAGE_MEDIA = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
}
_TEXT_EXTS = {".txt", ".md", ".markdown", ".csv", ".html", ".htm", ".json", ".eml"}
# Word/Excel/PowerPoint (and their OpenDocument/RTF cousins): the model can't read these
# binaries natively, so LibreOffice renders them to PDF, preserving tables/layout/images.
_OFFICE_EXTS = {
    ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
    ".odt", ".ods", ".odp", ".rtf",
}

# Closed vocabularies from the guide — the app rejects any value outside these.
_CONTENT_TYPES = {
    "title",
    "narrative",
    "bullet_list",
    "statistic",
    "table",
    "image_text",
    "section_divider",
}
_BLOCK_STATUSES = {"filled", "partial", "placeholder"}
_UNITS = {"usd", "usd_per_hour", "pct", "count", "score", "ratio", "date", "unknown"}

# The three data tiers, most-open to most-restricted. Single source of truth: the write
# path (ingest_store) and the web layer both import this so the closed set never drifts.
#   * internal  — shared with everyone (default; no identity required).
#   * private   — owned by the ingester; only they (plus break-glass admin) can read it.
#   * protected — group-scoped; only the single privileged Entra group can read it.
SENSITIVITIES = ("internal", "private", "protected")

# Fact fields the UI may edit that must be re-typed after a manual edit (the browser
# sends everything as strings). Kept next to the block int-fields the loader coerces.
_FACT_NUMBER_FIELDS = {"value_num", "confidence"}
# Fact int fields the UI may edit (source_block_index links a fact to its source block).
_FACT_INT_FIELDS = {"source_block_index"}

# A table caption shorter than this can't carry a subject + column names, so it won't embed
# well for retrieval — surfaced as a warning, not a hard failure.
_MIN_TABLE_CAPTION = 15

# Block fields that must be present and non-null (guide validation gates).
_REQUIRED_BLOCK_FIELDS = (
    "block_index",
    "section_number",
    "section_title",
    "section_theme",
    "block_order",
    "content_type",
    "block_status",
    "text_content",
)


class IngestError(RuntimeError):
    """Raised for an unsupported file type or an unusable model response."""


class StructuredResult(BaseModel):
    """The validated preview handed to the UI and (on confirm) to the loader."""

    manifest: dict[str, Any] = Field(default_factory=dict)
    blocks: list[dict[str, Any]] = Field(default_factory=list)
    facts: list[dict[str, Any]] = Field(default_factory=list)
    # Soft issues worth showing but not blocking the commit.
    warnings: list[str] = Field(default_factory=list)
    # Hard gate failures — a non-empty list disables the confirm button.
    errors: list[str] = Field(default_factory=list)
    # Diagnostics for the one structuring call: wall time, token usage, and estimated
    # cost — shown at the bottom of the preview (and kept with the draft).
    elapsed_ms: float = 0.0
    tokens: dict[str, int] = Field(default_factory=dict)
    cost_usd: float = 0.0

    @property
    def committable(self) -> bool:
        return not self.errors and bool(self.manifest.get("coverage_ok"))


def _find_soffice() -> str | None:
    """Locate the LibreOffice binary: an explicit SOFFICE_BIN override, then PATH, then
    the standard macOS app bundle. Returns None when LibreOffice isn't installed."""
    override = os.environ.get("SOFFICE_BIN")
    if override:
        return override
    for name in ("soffice", "libreoffice"):
        found = shutil.which(name)
        if found:
            return found
    mac_app = "/Applications/LibreOffice.app/Contents/MacOS/soffice"
    return mac_app if Path(mac_app).exists() else None


def _office_to_pdf(filename: str, raw: bytes) -> bytes:
    """Render an Office document to PDF bytes via headless LibreOffice, so it can take the
    native PDF path. A per-call UserInstallation profile keeps concurrent conversions (and
    a desktop LibreOffice the user may have open) from clashing. Raises IngestError with an
    actionable message when LibreOffice is missing or the conversion fails."""
    soffice = _find_soffice()
    if soffice is None:
        raise IngestError(
            "This server can't convert Office files (LibreOffice is not installed) — "
            "convert to PDF and retry."
        )
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / Path(filename).name
        src.write_bytes(raw)
        profile = Path(tmp) / "profile"
        try:
            proc = subprocess.run(
                [
                    soffice,
                    f"-env:UserInstallation=file://{profile}",
                    "--headless",
                    "--convert-to",
                    "pdf",
                    "--outdir",
                    tmp,
                    str(src),
                ],
                capture_output=True,
                timeout=120,
            )
        except subprocess.TimeoutExpired as e:
            raise IngestError("Office-to-PDF conversion timed out.") from e
        except OSError as e:
            raise IngestError(f"Could not run LibreOffice: {e}") from e

        pdf_path = src.with_suffix(".pdf")
        if proc.returncode != 0 or not pdf_path.exists():
            detail = proc.stderr.decode("utf-8", "replace").strip()[:200] or "unknown error"
            raise IngestError(f"Office-to-PDF conversion failed: {detail}")
        return pdf_path.read_bytes()


def file_content_block(filename: str, raw: bytes) -> dict[str, Any]:
    """One Anthropic content block for an uploaded file, so the model can read it.

    Office files are converted to PDF (via LibreOffice) first and then take the PDF path;
    PDFs become a `document` block, images an `image` block, text-family files a `text`
    block. Raises IngestError for unknown types (or when Office conversion isn't possible).

    Reused by two callers: the ingest structuring call (`build_content_block`) and the
    chat's context-attachment path (an uploaded doc the user wants the assistant to read).
    """
    ext = Path(filename).suffix.lower()
    if ext in _OFFICE_EXTS:
        # Convert in place: the model still sees the original filename for provenance.
        raw = _office_to_pdf(filename, raw)
        ext = ".pdf"
    if ext in _PDF_EXTS:
        return {
            "type": "document",
            "source": {
                "type": "base64",
                "media_type": "application/pdf",
                "data": base64.b64encode(raw).decode("ascii"),
            },
        }
    if ext in _IMAGE_MEDIA:
        return {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": _IMAGE_MEDIA[ext],
                "data": base64.b64encode(raw).decode("ascii"),
            },
        }
    if ext in _TEXT_EXTS:
        text = raw.decode("utf-8", errors="replace")
        return {"type": "text", "text": f"Filename: {filename}\n\n{text}"}
    raise IngestError(
        f"Unsupported file type '{ext or filename}' — convert to PDF and retry."
    )


def build_content_block(filename: str, raw: bytes) -> list[dict[str, Any]]:
    """Turn one uploaded file into Anthropic content blocks for the structuring call:
    the file itself plus the ingest instruction. Raises IngestError for unknown types."""
    source_block = file_content_block(filename, raw)
    instruction = {
        "type": "text",
        "text": (
            f'Structure this per the contract. source_file="{filename}". Return JSON only.'
        ),
    }
    return [source_block, instruction]


def _has_markdown_header(md: str) -> bool:
    """True if the Markdown table has a header separator row (e.g. ``| --- | --- |``).

    A pipe table's second line is a divider of dashes/colons/pipes; its presence is the
    cheapest signal that the header survived a page/slide split. Heuristic, not a guarantee.
    """
    for line in md.splitlines():
        stripped = line.strip().strip("|").strip()
        if not stripped or "-" not in stripped:
            continue
        if all(ch in "-: |" for ch in stripped):
            return True
    return False


def validate(data: dict[str, Any]) -> tuple[list[str], list[str]]:
    """Check a structured payload against the guide's gates.

    Returns (warnings, errors). Errors are hard failures that must block the commit;
    warnings are surfaced but non-blocking.
    """
    warnings: list[str] = []
    errors: list[str] = []

    manifest = data.get("manifest")
    if not isinstance(manifest, dict):
        errors.append("manifest is missing or not an object.")
        manifest = {}
    elif not manifest.get("coverage_ok"):
        errors.append("manifest.coverage_ok is not true — coverage incomplete, rejected.")

    blocks = data.get("blocks")
    if not isinstance(blocks, list):
        errors.append("blocks is missing or not a list.")
        blocks = []
    facts = data.get("facts")
    if not isinstance(facts, list):
        errors.append("facts is missing or not a list.")
        facts = []

    for i, block in enumerate(blocks):
        if not isinstance(block, dict):
            errors.append(f"block[{i}] is not an object.")
            continue
        for field in _REQUIRED_BLOCK_FIELDS:
            if block.get(field) is None:
                errors.append(f"block[{i}] missing required field '{field}'.")
        ct = block.get("content_type")
        if ct is not None and ct not in _CONTENT_TYPES:
            errors.append(f"block[{i}] content_type '{ct}' not in the closed set.")
        bs = block.get("block_status")
        if bs is not None and bs not in _BLOCK_STATUSES:
            errors.append(f"block[{i}] block_status '{bs}' not in the closed set.")
        # Table-specific gates: Markdown is the canonical serialization the retrieval and
        # query lanes read, so it is mandatory (hard gate). The caption and header checks are
        # heuristic quality signals surfaced as warnings — validate() only sees the finished
        # JSON, so it can't prove a table was split correctly, only nudge on findability.
        if ct == "table":
            md = block.get("table_markdown")
            if not isinstance(md, str) or not md.strip():
                errors.append(f"block[{i}] table block is missing table_markdown.")
            elif not _has_markdown_header(md):
                warnings.append(
                    f"block[{i}] table_markdown has no header row — a split may have dropped it."
                )
            caption = block.get("text_content")
            if isinstance(caption, str) and len(caption.strip()) < _MIN_TABLE_CAPTION:
                warnings.append(
                    f"block[{i}] table caption is very short — name its columns so it's findable."
                )

    for i, fact in enumerate(facts):
        if not isinstance(fact, dict):
            errors.append(f"fact[{i}] is not an object.")
            continue
        vn = fact.get("value_num")
        if not (vn is None or isinstance(vn, (int, float)) and not isinstance(vn, bool)):
            errors.append(f"fact[{i}] value_num must be a number or null, got {vn!r}.")
        unit = fact.get("unit")
        if unit not in _UNITS:
            errors.append(f"fact[{i}] unit '{unit}' not in the closed set.")
        raw_value = fact.get("raw_value")
        if not isinstance(raw_value, str) or not raw_value.strip():
            errors.append(f"fact[{i}] raw_value must be non-empty.")
        # Link back to the source block (block_index within the same ingest). Optional for
        # legacy/edited rows, so only type-check when present; the prompt is what makes the
        # model emit it. bool is an int subclass in Python — exclude it explicitly.
        sbi = fact.get("source_block_index")
        if not (sbi is None or (isinstance(sbi, int) and not isinstance(sbi, bool))):
            errors.append(f"fact[{i}] source_block_index must be an integer or null, got {sbi!r}.")

    # Carry the model's own manifest warnings through to the UI.
    for w in manifest.get("warnings", []) or []:
        warnings.append(str(w))

    return warnings, errors


def stamp_sensitivity(structured: "StructuredResult", tier: str) -> None:
    """Set every block's and fact's ``sensitivity`` field to ``tier`` in place.

    Called right after structuring so the model's own output can never decide a row's
    tier — the server overwrites all of them with the author's chosen default. The
    post-structure editor then refines individual rows through the validated /edit gate.
    """
    if tier not in SENSITIVITIES:
        tier = "internal"
    for rec in structured.blocks:
        rec["sensitivity"] = tier
    for rec in structured.facts:
        rec["sensitivity"] = tier


def _to_number(value: Any) -> Any:
    """Coerce a browser-supplied string to int/float; blank -> None; leave others as-is."""
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return None
        try:
            return int(s)
        except ValueError:
            try:
                return float(s)
            except ValueError:
                return s
    return value


def coerce_for_validation(
    blocks: list[dict[str, Any]], facts: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Re-type manually edited rows so ``validate`` behaves like the model path.

    The browser sends all cell values as strings; this restores the numeric/None shapes
    the validator (and later the loader) expect: block int fields, fact numeric fields,
    and blank strings on optional fields collapse to None. Returns new lists; inputs are
    not mutated. Unknown keys pass through untouched.
    """
    int_fields = {"block_index", "section_number", "block_order"}
    out_blocks: list[dict[str, Any]] = []
    for b in blocks:
        rec = dict(b)
        for f in int_fields:
            if f in rec:
                rec[f] = _to_number(rec[f])
        out_blocks.append(rec)
    out_facts: list[dict[str, Any]] = []
    for f in facts:
        rec = dict(f)
        for k in _FACT_NUMBER_FIELDS | _FACT_INT_FIELDS:
            if k in rec:
                rec[k] = _to_number(rec[k])
        out_facts.append(rec)
    return out_blocks, out_facts


def tier_requirements(tiers: Iterable[str]) -> tuple[bool, bool]:
    """From the tiers present in a payload, what must the caller prove?

    Returns ``(needs_identity, needs_group)``:
      * ``needs_identity`` — any row is private or protected (confidential rows must be
        owned, so the caller must resolve to an identity).
      * ``needs_group`` — any row is protected (only the privileged group may author it).
    """
    tier_set = set(tiers)
    needs_group = "protected" in tier_set
    needs_identity = needs_group or "private" in tier_set
    return needs_identity, needs_group


def _registry_block(registry: dict[str, list[str]] | None) -> dict[str, Any] | None:
    """Render the known-vocabulary registry as a system text block, or None if empty.

    The guide promises "the app passes a registry of known entity_types/attributes in
    context"; this is that block. It's appended AFTER the cached guide so the guide prefix
    stays cache-eligible, and is itself left uncached because the vocabulary grows with
    every ingest.
    """
    if not registry:
        return None
    lines = [
        "## Known vocabulary registry (from prior ingests)",
        "",
        "Before minting a NEW entity_type or attribute, check the lists below and REUSE an "
        "existing value when one fits by meaning (not just exact spelling) — this prevents "
        'drift like "revenue" vs "rev". Only mint a new value when nothing here fits.',
        "",
    ]
    labels = {"entity_type": "Known entity_types", "attribute": "Known attributes"}
    for field, label in labels.items():
        values = registry.get(field)
        if values:
            lines.append(f"{label}: {', '.join(values)}")
    return {"type": "text", "text": "\n".join(lines)}


# Document-level provenance the app stamps itself instead of having the model repeat it on
# every row. These four are identical for a whole upload, so emitting them per block/fact was
# pure output-token waste — on a fine-grained (sentence-per-block) doc they were ~40% of the
# blocks' output. The app is also the *authoritative* source: it knows the real filename, how it
# delivered the bytes to the model, and the run date — no model typo can corrupt provenance.
_OPTIONAL_BLOCK_STRINGS = (
    "image_class",
    "table_markdown",
    "table_html",
    "image_ocr_text",
    "owner",
)


def _source_parser_for(filename: str) -> str:
    """How the app fed this file to the model (its provenance 'parser'). Office and PDF both
    reach the model as rendered PDF pages (vision); images as vision/OCR; text-family inline."""
    ext = Path(filename).suffix.lower()
    if ext in _OFFICE_EXTS or ext in _PDF_EXTS:
        return "pdf_vision"
    if ext in _IMAGE_MEDIA:
        return "ocr"
    return "text"


def _apply_document_metadata(
    manifest: dict[str, Any],
    blocks: list[dict[str, Any]],
    facts: list[dict[str, Any]],
    filename: str,
) -> None:
    """Stamp document-level provenance onto every row in place, so the model no longer has to.

    `source_file`, `source_parser`, and `extracted_at` are app-authoritative (the app knows the
    real name, transport, and run date) and overwrite any value the model emitted.
    `source_modified_at` is the document's own last-modified — the model reads it from the file
    metadata and reports it once in the manifest, so it's filled from there onto rows that omit
    it (a per-row value still wins). Optional string fields default to "" so a lean model output
    (which omits empty fields) still hands the preview table and the loader a stable shape.
    """
    source_file = filename or str(manifest.get("source_file") or "")
    source_parser = _source_parser_for(filename)
    extracted_at = date.today().isoformat()
    modified_at = manifest.get("source_modified_at")

    for b in blocks:
        if not isinstance(b, dict):
            continue
        b["source_file"] = source_file
        b["source_parser"] = source_parser
        b["extracted_at"] = extracted_at
        if not b.get("source_modified_at") and modified_at:
            b["source_modified_at"] = modified_at
        for f in _OPTIONAL_BLOCK_STRINGS:
            if not b.get(f):
                b[f] = ""
    for fact in facts:
        if isinstance(fact, dict):
            fact["source_file"] = source_file


def structure_upload(
    client: anthropic.Anthropic,
    config: AgentConfig,
    filename: str,
    raw: bytes,
    registry: dict[str, list[str]] | None = None,
) -> StructuredResult:
    """One-shot structuring call: file bytes -> validated StructuredResult.

    ``registry`` is the known-vocabulary map (entity_type/attribute values already in the
    facts table); when present it's appended to the system prompt so the model reuses an
    existing value instead of minting a near-duplicate. Absent -> the model runs on the
    guide alone (first upload into an empty warehouse, or a degraded registry read).

    Raises IngestError for unsupported types or when the model output can't be used
    (max_tokens truncation, non-JSON). Validation failures do NOT raise — they come
    back inside the result's `errors` so the UI can show the preview and block commit.
    """
    content = build_content_block(filename, raw)
    system = INGEST_SYSTEM
    block = _registry_block(registry)
    if block is not None:
        system = INGEST_SYSTEM + [block]
    # Streaming: a large document can emit tens of thousands of output tokens, and the
    # SDK refuses a non-streaming call whose max_tokens could exceed the 10-minute cap.
    start = time.perf_counter()
    with client.messages.stream(
        model=config.model,
        max_tokens=config.ingest_max_tokens,
        system=system,
        messages=[{"role": "user", "content": content}],
    ) as stream:
        resp = stream.get_final_message()
    elapsed_ms = (time.perf_counter() - start) * 1000

    # Meter the call the same way the agent loop does, so ingest cost is comparable.
    usage = usage_from(resp)
    cost_usd = _estimate_cost(usage)
    tokens = {**usage, "total": sum(usage.values())}

    if resp.stop_reason == "max_tokens":
        raise IngestError(
            "The document was too large to structure in one pass (output truncated). "
            "Split it into smaller files and retry."
        )

    text = "".join(b.text for b in resp.content if getattr(b, "type", None) == "text")
    try:
        data = _extract_json(text)
    except Exception as e:  # noqa: BLE001 — surface any parse failure as an ingest error
        raise IngestError(f"Model did not return usable JSON: {e}") from e

    manifest = data.get("manifest") or {}
    blocks = data.get("blocks") or []
    facts = data.get("facts") or []
    # Back-fill the provenance the model no longer repeats per row, and normalize optional
    # empties, BEFORE validating — so the gates and the loader see complete, stable rows.
    _apply_document_metadata(manifest, blocks, facts, filename)

    warnings, errors = validate({"manifest": manifest, "blocks": blocks, "facts": facts})
    return StructuredResult(
        manifest=manifest,
        blocks=blocks,
        facts=facts,
        warnings=warnings,
        errors=errors,
        elapsed_ms=elapsed_ms,
        tokens=tokens,
        cost_usd=cost_usd,
    )
