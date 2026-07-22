"""Offline tests for the ingest row loader (sf_agent.ingest_store).

No real Snowflake — a fake connection captures the SQL and bound rows so we can
assert column order, ingest_id injection, and type coercion without a warehouse.
"""

from sf_agent.ingest import StructuredResult, _registry_block
from sf_agent.ingest_store import BLOCK_COLS, FACT_COLS, ensure_tables, read_registry, write
from sf_agent.types import QueryResult


class FakeConn:
    def __init__(self):
        self.ddl = []
        self.inserts = []  # (sql, rows)

    def execute_ddl(self, sql):
        self.ddl.append(sql)

    def executemany(self, sql, rows):
        self.inserts.append((sql, rows))
        return len(rows)


class FakeReadConn:
    """A read connection that returns canned distinct values (or raises) per registry field.

    `responses` maps a field name ("entity_type"/"attribute") to either the list-of-rows the
    SELECT should return, or an Exception to raise — so we can drive both the happy path and
    the degrade-gracefully path without a warehouse.
    """

    def __init__(self, responses):
        self.responses = responses
        self.queries = []

    def execute(self, sql, max_rows=None):
        self.queries.append((sql, max_rows))
        for field, resp in self.responses.items():
            if f"SELECT {field} " in sql:
                if isinstance(resp, Exception):
                    raise resp
                return QueryResult(
                    columns=[field], rows=resp, row_count=len(resp), truncated=False
                )
        return QueryResult(columns=[], rows=[], row_count=0, truncated=False)


def test_read_registry_maps_each_field_most_used_first():
    conn = FakeReadConn(
        {
            "entity_type": [["client"], ["competitor"]],
            "attribute": [["revenue"], ["gm_pct"]],
        }
    )
    reg = read_registry(conn)
    assert reg == {
        "entity_type": ["client", "competitor"],
        "attribute": ["revenue", "gm_pct"],
    }
    # The frequency ordering is pushed into SQL, and the cap is honored as a row limit.
    assert all("ORDER BY COUNT(*) DESC" in sql for sql, _ in conn.queries)
    assert all(cap == 100 for _, cap in conn.queries)


def test_read_registry_omits_empty_fields():
    conn = FakeReadConn({"entity_type": [["client"]], "attribute": []})
    reg = read_registry(conn)
    assert reg == {"entity_type": ["client"]}


def test_read_registry_degrades_when_a_query_raises():
    # A missing `facts` table (fresh warehouse) or transient error must not block ingest:
    # the failing field is dropped and the readable field still comes back.
    conn = FakeReadConn(
        {"entity_type": RuntimeError("no such table"), "attribute": [["revenue"]]}
    )
    reg = read_registry(conn)
    assert reg == {"attribute": ["revenue"]}


def test_registry_block_is_none_when_empty():
    assert _registry_block(None) is None
    assert _registry_block({}) is None


def test_registry_block_lists_known_values_with_reuse_instruction():
    block = _registry_block({"entity_type": ["client"], "attribute": ["revenue", "gm_pct"]})
    assert block is not None and block["type"] == "text"
    text = block["text"]
    assert "REUSE" in text
    assert "Known entity_types: client" in text
    assert "Known attributes: revenue, gm_pct" in text
    # No cache_control: the registry grows per ingest, so it trails the cached guide uncached.
    assert "cache_control" not in block


def _structured():
    block = {
        "block_index": 2.0,  # float that must be cast to int
        "section_number": 1,
        "section_title": "Overview",
        "section_theme": "intro",
        "block_order": 0,
        "content_type": "narrative",
        "block_status": "filled",
        "text_content": "prose",
        "source_modified_at": "",  # empty date -> NULL
    }
    fact = {
        "fact_id": "abc",
        "source_file": "deck.pdf",
        "entity_name": "HPE",
        "attribute": "revenue",
        "value_num": 1800000,
        "unit": "usd",
        "raw_value": "$1.8M",
        "source_block_index": "3",  # string that must be cast to int
    }
    return StructuredResult(manifest={"coverage_ok": True}, blocks=[block], facts=[fact])


def test_column_counts_match_the_guide():
    # 17 guide block fields + ingest_id + sensitivity + sensitivity_category + ingested_by;
    # 14 guide fact fields (incl. source_block_index) + the same 4 server-set columns.
    assert len(BLOCK_COLS) == 21
    assert len(FACT_COLS) == 18
    # ingest_id precedes the server-set tier/ownership columns, which trail the tuple.
    assert BLOCK_COLS[-4:] == ("ingest_id", "sensitivity", "sensitivity_category", "ingested_by")
    assert FACT_COLS[-4:] == ("ingest_id", "sensitivity", "sensitivity_category", "ingested_by")


def test_ensure_tables_creates_then_backfills_idempotently():
    conn = FakeConn()
    ensure_tables(conn)
    creates = [s for s in conn.ddl if "CREATE TABLE IF NOT EXISTS" in s]
    alters = [s for s in conn.ddl if "ALTER TABLE" in s]
    # Two CREATEs (blocks + facts), each idempotent.
    assert len(creates) == 2
    assert any(" blocks " in s for s in creates)
    assert any(" facts " in s for s in creates)
    # Additive columns are backfilled with IF NOT EXISTS so old tables self-heal.
    assert alters, "expected additive ALTER TABLE ... ADD COLUMN statements"
    assert all("ADD COLUMN IF NOT EXISTS" in s for s in alters)
    # The new fact->block link column is among them.
    assert any("facts" in s and "source_block_index" in s for s in alters)


def test_write_inserts_blocks_then_facts_with_counts():
    conn = FakeConn()
    b, f = write(conn, _structured(), "ing123")
    assert (b, f) == (1, 1)
    assert len(conn.inserts) == 2
    blocks_sql, block_rows = conn.inserts[0]
    facts_sql, fact_rows = conn.inserts[1]
    assert "INSERT INTO blocks" in blocks_sql
    assert "INSERT INTO facts" in facts_sql
    # One %s placeholder per column.
    assert blocks_sql.count("%s") == len(BLOCK_COLS)
    assert facts_sql.count("%s") == len(FACT_COLS)


def test_write_injects_ingest_id_and_coerces_types():
    conn = FakeConn()
    write(conn, _structured(), "ing123")
    _, block_rows = conn.inserts[0]
    row = block_rows[0]
    assert len(row) == len(BLOCK_COLS)
    # ingest_id is stamped by column name (now third-from-last, before the ownership cols).
    assert row[BLOCK_COLS.index("ingest_id")] == "ing123"
    # Unspecified ownership defaults: shared + no owner.
    assert row[BLOCK_COLS.index("sensitivity")] == "internal"
    # Friendly category mirrors the enum: the default `internal` tier reads as "general".
    assert row[BLOCK_COLS.index("sensitivity_category")] == "general"
    assert row[BLOCK_COLS.index("ingested_by")] is None
    # block_index (float 2.0) coerced to int.
    assert row[BLOCK_COLS.index("block_index")] == 2
    assert isinstance(row[BLOCK_COLS.index("block_index")], int)
    # Empty date string -> None.
    assert row[BLOCK_COLS.index("source_modified_at")] is None
    # Missing optional field -> None (not KeyError).
    assert row[BLOCK_COLS.index("owner")] is None


def test_write_fact_row_carries_value_and_ingest_id():
    conn = FakeConn()
    write(conn, _structured(), "ing123")
    _, fact_rows = conn.inserts[1]
    row = fact_rows[0]
    assert row[FACT_COLS.index("value_num")] == 1800000
    assert row[FACT_COLS.index("raw_value")] == "$1.8M"
    assert row[FACT_COLS.index("ingest_id")] == "ing123"
    # Fact->block link ("3") coerced to a clean int for the NUMBER column.
    sbi = row[FACT_COLS.index("source_block_index")]
    assert sbi == 3 and isinstance(sbi, int)


def test_write_empty_payload_is_a_noop_zero():
    conn = FakeConn()
    empty = StructuredResult(manifest={"coverage_ok": True}, blocks=[], facts=[])
    b, f = write(conn, empty, "ing0")
    assert (b, f) == (0, 0)


def test_write_honors_per_row_sensitivity():
    # A payload edited to mixed tiers: each row is stamped from its own server-vetted
    # `sensitivity` field, and the owner is stamped only on the confidential rows.
    conn = FakeConn()
    structured = StructuredResult(
        manifest={"coverage_ok": True},
        blocks=[
            {"block_index": 0, "text_content": "shared", "sensitivity": "internal"},
            {"block_index": 1, "text_content": "mine", "sensitivity": "private"},
        ],
        facts=[],
    )
    write(conn, structured, "ing9", ingested_by="alice@bayone.com")
    _, block_rows = conn.inserts[0]
    s_idx = BLOCK_COLS.index("sensitivity")
    c_idx = BLOCK_COLS.index("sensitivity_category")
    o_idx = BLOCK_COLS.index("ingested_by")
    # Internal row stays shared and unowned; private row carries the caller as owner.
    assert block_rows[0][s_idx] == "internal" and block_rows[0][o_idx] is None
    assert block_rows[1][s_idx] == "private" and block_rows[1][o_idx] == "alice@bayone.com"
    # The friendly category tracks each row: internal -> "general", private -> "private".
    assert block_rows[0][c_idx] == "general"
    assert block_rows[1][c_idx] == "private"


def test_write_falls_back_to_default_for_unknown_row_tier():
    # A row whose `sensitivity` isn't in the closed set falls back to the write default.
    conn = FakeConn()
    structured = StructuredResult(
        manifest={"coverage_ok": True},
        blocks=[{"block_index": 0, "sensitivity": "bogus"}],
        facts=[],
    )
    write(conn, structured, "ing9", sensitivity="internal", ingested_by="alice@bayone.com")
    _, block_rows = conn.inserts[0]
    assert block_rows[0][BLOCK_COLS.index("sensitivity")] == "internal"
    assert block_rows[0][BLOCK_COLS.index("ingested_by")] is None
