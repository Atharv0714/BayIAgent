"""On-disk persistence for the ingest history sidebar.

Every structured upload is written as a JSON record so it appears in the ingest
history and survives a server restart. A record starts as a ``draft`` (structured,
not yet confirmed) and becomes ``committed`` once loaded into Snowflake; discarding
deletes it. Un-confirmed drafts can be resumed and committed later.

Each record is one file `<ingest_id>.json` holding the source filename, a UTC
`created_at`, a `status`, commit metadata, and the serialized StructuredResult. All
functions are pure over a directory path so they are trivially testable offline.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from sf_agent.ingest import StructuredResult

logger = logging.getLogger("sf_agent.ingest_drafts")


def _path(directory: Path, ingest_id: str) -> Path:
    return directory / f"{ingest_id}.json"


def save(directory: Path, ingest_id: str, source_file: str, result: StructuredResult) -> str:
    """Write a record as a ``draft`` on disk; returns its UTC `created_at` ISO timestamp."""
    directory.mkdir(parents=True, exist_ok=True)
    created_at = datetime.now(timezone.utc).isoformat()
    payload = {
        "ingest_id": ingest_id,
        "source_file": source_file,
        "created_at": created_at,
        "status": "draft",
        "committed_at": None,
        "blocks_written": None,
        "facts_written": None,
        "result": result.model_dump(),
    }
    _path(directory, ingest_id).write_text(json.dumps(payload), "utf-8")
    return created_at


def mark_committed(
    directory: Path, ingest_id: str, blocks_written: int, facts_written: int
) -> bool:
    """Flip a record from ``draft`` to ``committed`` with commit metadata. True if it existed."""
    p = _path(directory, ingest_id)
    if not p.exists():
        return False
    try:
        data = json.loads(p.read_text("utf-8"))
    except Exception:  # noqa: BLE001 — a corrupt file can't be committed-marked
        return False
    data["status"] = "committed"
    data["committed_at"] = datetime.now(timezone.utc).isoformat()
    data["blocks_written"] = blocks_written
    data["facts_written"] = facts_written
    p.write_text(json.dumps(data), "utf-8")
    return True


def load_all(directory: Path) -> dict[str, StructuredResult]:
    """Rehydrate only un-committed drafts keyed by ingest_id (committable after restart)."""
    out: dict[str, StructuredResult] = {}
    if not directory.exists():
        return out
    for f in sorted(directory.glob("*.json")):
        try:
            data = json.loads(f.read_text("utf-8"))
            if data.get("status", "draft") != "draft":
                continue
            out[data["ingest_id"]] = StructuredResult(**data["result"])
        except Exception:  # noqa: BLE001 — a corrupt draft must not block startup
            logger.warning("ingest_drafts: skipping unreadable draft %s", f.name)
    return out


def summaries(directory: Path) -> list[dict]:
    """Lightweight list for the history sidebar — newest first, no block/fact bodies."""
    rows: list[dict] = []
    if not directory.exists():
        return rows
    for f in directory.glob("*.json"):
        try:
            data = json.loads(f.read_text("utf-8"))
        except Exception:  # noqa: BLE001
            continue
        r = data.get("result", {})
        rows.append(
            {
                "ingest_id": data.get("ingest_id"),
                "source_file": data.get("source_file"),
                "created_at": data.get("created_at"),
                "status": data.get("status", "draft"),
                "committed_at": data.get("committed_at"),
                "blocks_written": data.get("blocks_written"),
                "facts_written": data.get("facts_written"),
                "block_count": len(r.get("blocks", []) or []),
                "fact_count": len(r.get("facts", []) or []),
                "error_count": len(r.get("errors", []) or []),
            }
        )
    rows.sort(key=lambda x: x.get("created_at") or "", reverse=True)
    return rows


def get(directory: Path, ingest_id: str) -> dict | None:
    """Return the full stored draft record (with its serialized result), or None."""
    p = _path(directory, ingest_id)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text("utf-8"))
    except Exception:  # noqa: BLE001
        return None


def delete(directory: Path, ingest_id: str) -> bool:
    """Remove a draft file. Returns True if it existed."""
    p = _path(directory, ingest_id)
    if p.exists():
        p.unlink()
        return True
    return False
