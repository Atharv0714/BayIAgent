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
from sf_agent.ingest import StructuredResult

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
    "ingest_id",
)

# Integer block fields cast on the way in so a JSON float ("2.0") lands as an int.
_BLOCK_INT_FIELDS = {"block_index", "section_number", "block_order"}
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
    ingest_id VARCHAR
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
    ingest_id VARCHAR
)
"""


def ensure_tables(conn: SnowflakeConnection) -> None:
    """Create `blocks` and `facts` if they don't already exist (idempotent)."""
    conn.execute_ddl(_CREATE_BLOCKS)
    conn.execute_ddl(_CREATE_FACTS)


def _insert_sql(table: str, cols: tuple[str, ...]) -> str:
    placeholders = ", ".join(["%s"] * len(cols))
    return f'INSERT INTO {table} ({", ".join(cols)}) VALUES ({placeholders})'


def _coerce_block(value: Any, col: str) -> Any:
    if value is None:
        return None
    if col in _BLOCK_INT_FIELDS:
        try:
            return int(value)
        except (TypeError, ValueError):
            return None
    if col in _DATE_FIELDS and isinstance(value, str) and not value.strip():
        return None
    return value


def _row_tuple(record: dict[str, Any], cols: tuple[str, ...], ingest_id: str) -> tuple[Any, ...]:
    values: list[Any] = []
    for col in cols:
        if col == "ingest_id":
            values.append(ingest_id)
        else:
            values.append(_coerce_block(record.get(col), col))
    return tuple(values)


def write(conn: SnowflakeConnection, structured: StructuredResult, ingest_id: str) -> tuple[int, int]:
    """Load a validated payload's blocks then facts, tagged with `ingest_id`.

    Returns (blocks_written, facts_written). Each table is inserted under its own
    commit inside `executemany`.
    """
    block_rows = [_row_tuple(b, BLOCK_COLS, ingest_id) for b in structured.blocks]
    fact_rows = [_row_tuple(f, FACT_COLS, ingest_id) for f in structured.facts]

    blocks_written = conn.executemany(_insert_sql("blocks", BLOCK_COLS), block_rows)
    facts_written = conn.executemany(_insert_sql("facts", FACT_COLS), fact_rows)
    return blocks_written, facts_written
