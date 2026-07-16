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
    # 17 guide block fields + ingest_id; 13 guide fact fields + ingest_id.
    assert len(BLOCK_COLS) == 18
    assert len(FACT_COLS) == 14
    assert BLOCK_COLS[-1] == "ingest_id"
    assert FACT_COLS[-1] == "ingest_id"


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
    # ingest_id is the last column.
    assert row[-1] == "ing123"
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
    assert row[-1] == "ing123"


def test_write_empty_payload_is_a_noop_zero():
    conn = FakeConn()
    empty = StructuredResult(manifest={"coverage_ok": True}, blocks=[], facts=[])
    b, f = write(conn, empty, "ing0")
    assert (b, f) == (0, 0)
