"""Offline tests for the ingest row loader (sf_agent.ingest_store).

No real Snowflake — a fake connection captures the SQL and bound rows so we can
assert column order, ingest_id injection, and type coercion without a warehouse.
"""

from sf_agent.ingest import StructuredResult
from sf_agent.ingest_store import BLOCK_COLS, FACT_COLS, ensure_tables, write


class FakeConn:
    def __init__(self):
        self.ddl = []
        self.inserts = []  # (sql, rows)

    def execute_ddl(self, sql):
        self.ddl.append(sql)

    def executemany(self, sql, rows):
        self.inserts.append((sql, rows))
        return len(rows)


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
    }
    return StructuredResult(manifest={"coverage_ok": True}, blocks=[block], facts=[fact])


def test_column_counts_match_the_guide():
    # 17 guide block fields + ingest_id + sensitivity + sensitivity_category + ingested_by;
    # 13 guide fact fields + ingest_id + sensitivity + sensitivity_category + ingested_by.
    assert len(BLOCK_COLS) == 21
    assert len(FACT_COLS) == 17
    # ingest_id precedes the server-set tier/ownership columns, which trail the tuple.
    assert BLOCK_COLS[-4:] == ("ingest_id", "sensitivity", "sensitivity_category", "ingested_by")
    assert FACT_COLS[-4:] == ("ingest_id", "sensitivity", "sensitivity_category", "ingested_by")


def test_ensure_tables_runs_two_idempotent_ddls():
    conn = FakeConn()
    ensure_tables(conn)
    assert len(conn.ddl) == 2
    assert all("CREATE TABLE IF NOT EXISTS" in s for s in conn.ddl)
    assert any(" blocks " in s for s in conn.ddl)
    assert any(" facts " in s for s in conn.ddl)


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
