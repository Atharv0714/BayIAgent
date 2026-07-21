"""Idempotent table creation + parametrized row loader for the ingest write path.

The two fixed tables (`blocks`, `facts`) are created once and never change per
upload. On commit, the validated StructuredResult is turned into row tuples in a
fixed column order and bulk-inserted via bound parameters — the SQL is entirely
app-authored (identifiers fixed, values bound), never model text, which is why it
runs outside the read-only guard.
"""

from __future__ import annotations

from typing import Any

from sf_agent.connection import SnowflakeConnection
from sf_agent.ingest import SENSITIVITIES, StructuredResult

# Fixed column order for `blocks` (17 guide fields + ingest_id). `id` is a surrogate
# the table auto-assigns and is NOT written here.
BLOCK_COLS = (
    "block_index",
    "section_number",
    "section_title",
    "section_theme",
    "block_order",
    "content_type",
    "block_status",
    "image_class",
    "text_content",
    "table_html",
    "table_markdown",
    "image_ocr_text",
    "owner",
    "source_parser",
    "source_file",
    "source_modified_at",
    "extracted_at",
    "ingest_id",
    "sensitivity",
    "sensitivity_category",
    "ingested_by",
)

# Fixed column order for `facts` (13 guide fields + ingest_id).
FACT_COLS = (
    "fact_id",
    "source_file",
    "source_locator",
    "entity_type",
    "entity_name",
    "attribute",
    "period",
    "value_num",
    "unit",
    "value_text",
    "raw_value",
    "confidence",
    "notes",
    "source_block_index",
    "ingest_id",
    "sensitivity",
    "sensitivity_category",
    "ingested_by",
)

# Integer block fields cast on the way in so a JSON float ("2.0") lands as an int.
_BLOCK_INT_FIELDS = {"block_index", "section_number", "block_order"}
# Integer fact fields cast the same way. source_block_index points back at the block a fact
# was extracted from (block_index within the same ingest), so it must land as a clean int.
_FACT_INT_FIELDS = {"source_block_index"}
# Date-typed columns; empty string -> NULL so DATE parsing doesn't choke.
_DATE_FIELDS = {"source_modified_at", "extracted_at"}

_CREATE_BLOCKS = """
CREATE TABLE IF NOT EXISTS blocks (
    id NUMBER AUTOINCREMENT PRIMARY KEY,
    block_index NUMBER,
    section_number NUMBER,
    section_title VARCHAR,
    section_theme VARCHAR,
    block_order NUMBER,
    content_type VARCHAR,
    block_status VARCHAR,
    image_class VARCHAR,
    text_content VARCHAR,
    table_html VARCHAR,
    table_markdown VARCHAR,
    image_ocr_text VARCHAR,
    owner VARCHAR,
    source_parser VARCHAR,
    source_file VARCHAR,
    source_modified_at DATE,
    extracted_at DATE,
    ingest_id VARCHAR,
    sensitivity VARCHAR DEFAULT 'internal',
    sensitivity_category VARCHAR DEFAULT 'general',
    ingested_by VARCHAR
)
"""

_CREATE_FACTS = """
CREATE TABLE IF NOT EXISTS facts (
    id NUMBER AUTOINCREMENT PRIMARY KEY,
    fact_id VARCHAR,
    source_file VARCHAR,
    source_locator VARCHAR,
    entity_type VARCHAR,
    entity_name VARCHAR,
    attribute VARCHAR,
    period VARCHAR,
    value_num NUMBER,
    unit VARCHAR,
    value_text VARCHAR,
    raw_value VARCHAR,
    confidence FLOAT,
    notes VARCHAR,
    source_block_index NUMBER,
    ingest_id VARCHAR,
    sensitivity VARCHAR DEFAULT 'internal',
    sensitivity_category VARCHAR DEFAULT 'general',
    ingested_by VARCHAR
)
"""


# Columns added to the fixed schema after a table may already exist in a live warehouse.
# `CREATE TABLE IF NOT EXISTS` is a no-op on an existing table, so it never backfills a new
# column — an INSERT with the current column list would then fail against the old table. Each
# entry is applied with `ADD COLUMN IF NOT EXISTS`, so this self-heals older deployments and
# stays a no-op once the column is present. Append here whenever the fixed schema grows.
_ADDITIVE_COLUMNS: tuple[tuple[str, str, str], ...] = (
    ("blocks", "sensitivity", "VARCHAR DEFAULT 'internal'"),
    ("blocks", "sensitivity_category", "VARCHAR DEFAULT 'general'"),
    ("blocks", "ingested_by", "VARCHAR"),
    ("facts", "source_block_index", "NUMBER"),
    ("facts", "sensitivity", "VARCHAR DEFAULT 'internal'"),
    ("facts", "sensitivity_category", "VARCHAR DEFAULT 'general'"),
    ("facts", "ingested_by", "VARCHAR"),
)


def ensure_tables(conn: SnowflakeConnection) -> None:
    """Create `blocks` and `facts` if absent, then backfill any additive columns (idempotent).

    Fresh warehouses get the full schema from the CREATE statements; warehouses whose tables
    predate a column get it via `ALTER TABLE ADD COLUMN IF NOT EXISTS`. Both paths are safe to
    run on every startup.
    """
    conn.execute_ddl(_CREATE_BLOCKS)
    conn.execute_ddl(_CREATE_FACTS)
    for table, column, coltype in _ADDITIVE_COLUMNS:
        conn.execute_ddl(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column} {coltype}")


def _insert_sql(table: str, cols: tuple[str, ...]) -> str:
    placeholders = ", ".join(["%s"] * len(cols))
    return f'INSERT INTO {table} ({", ".join(cols)}) VALUES ({placeholders})'


def _coerce_block(value: Any, col: str) -> Any:
    if value is None:
        return None
    if col in _BLOCK_INT_FIELDS or col in _FACT_INT_FIELDS:
        try:
            return int(value)
        except (TypeError, ValueError):
            return None
    if col in _DATE_FIELDS and isinstance(value, str) and not value.strip():
        return None
    return value


def _row_tuple(
    record: dict[str, Any], cols: tuple[str, ...], injected: dict[str, Any]
) -> tuple[Any, ...]:
    """Build a row tuple in `cols` order. Server-set columns (ingest_id, sensitivity,
    sensitivity_category, ingested_by) come from `injected`; everything else from the
    validated record."""
    values: list[Any] = []
    for col in cols:
        if col in injected:
            values.append(injected[col])
        else:
            values.append(_coerce_block(record.get(col), col))
    return tuple(values)


def _injected_for(rec: dict[str, Any], ingest_id: str, default_tier: str, ingested_by: str | None):
    """Per-row server-set columns: honor the row's own (server-vetted) `sensitivity`
    when present, else fall back to `default_tier`. `ingested_by` is stamped only on
    confidential rows so internal rows stay shared and unowned."""
    tier = rec.get("sensitivity")
    if tier not in SENSITIVITIES:
        tier = default_tier
    return {
        "ingest_id": ingest_id,
        "sensitivity": tier,
        # Human-facing label mirroring the enforcement enum: the default `internal`
        # tier reads as "general"; the confidential tiers keep their own name.
        "sensitivity_category": "general" if tier == "internal" else tier,
        "ingested_by": ingested_by if tier != "internal" else None,
    }


def write(
    conn: SnowflakeConnection,
    structured: StructuredResult,
    ingest_id: str,
    sensitivity: str = "internal",
    ingested_by: str | None = None,
) -> tuple[int, int]:
    """Load a validated payload's blocks then facts, tagged with `ingest_id`.

    Each row's tier comes from its own server-vetted `sensitivity` field (set by the
    structure/edit endpoints), falling back to the `sensitivity` default for rows that
    carry none — so a uniform-tier upload and a per-row-edited one both work. `ingested_by`
    (the caller's identity) is stamped by the server only on confidential rows, never
    taken from the model output, so the row-access policy can isolate an owner's data.
    Defaults keep a row shared and unowned, matching pre-feature behavior.

    Returns (blocks_written, facts_written). Each table is inserted under its own
    commit inside `executemany`.
    """
    block_rows = [
        _row_tuple(b, BLOCK_COLS, _injected_for(b, ingest_id, sensitivity, ingested_by))
        for b in structured.blocks
    ]
    fact_rows = [
        _row_tuple(f, FACT_COLS, _injected_for(f, ingest_id, sensitivity, ingested_by))
        for f in structured.facts
    ]

    blocks_written = conn.executemany(_insert_sql("blocks", BLOCK_COLS), block_rows)
    facts_written = conn.executemany(_insert_sql("facts", FACT_COLS), fact_rows)
    return blocks_written, facts_written
