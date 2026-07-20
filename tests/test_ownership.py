"""Unit tests for the per-owner private-data feature (no Snowflake required).

Covers the three server-side guarantees that make private data isolable:
  * the INSERT column lists carry `sensitivity` + `ingested_by` and stay in lockstep
    with the CREATE TABLE definitions (a mismatch would misalign every bound row);
  * `write()` stamps those two columns from its arguments, never from the model payload;
  * the web caller-identity resolver prefers the Easy Auth header over the dev fallback;
  * the read-only guard still rejects the `SET` the app uses to bind identity, proving
    a model-authored query can't set the session variable itself.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from sf_agent import ingest_store
from sf_agent.config import AuthConfig
from sf_agent.connection import SnowflakeConnection
from sf_agent.ingest_store import BLOCK_COLS, FACT_COLS, _row_tuple, write
from sf_agent.sql_guard import SqlGuardError, assert_read_only


# ── column lists match the tables ───────────────────────────────────────────
def test_new_columns_present_and_last() -> None:
    for cols in (BLOCK_COLS, FACT_COLS):
        assert cols[-3:] == ("sensitivity", "sensitivity_category", "ingested_by")


def test_insert_cols_match_create_table_columns() -> None:
    # No nested parens in the DDL, so top-level commas == (columns - 1) == len(cols),
    # since the INSERT lists exclude only the auto-assigned `id`.
    assert ingest_store._CREATE_BLOCKS.count(",") == len(BLOCK_COLS)
    assert ingest_store._CREATE_FACTS.count(",") == len(FACT_COLS)
    assert "sensitivity VARCHAR DEFAULT 'internal'" in ingest_store._CREATE_BLOCKS
    assert "sensitivity VARCHAR DEFAULT 'internal'" in ingest_store._CREATE_FACTS


# ── _row_tuple injects server-set values, reads the rest from the record ─────
def test_row_tuple_stamps_injected_values() -> None:
    injected = {"ingest_id": "ig1", "sensitivity": "private", "ingested_by": "alice@bayone.com"}
    rec = {"block_index": 3, "text_content": "hello", "owner": "Doc Author"}
    row = _row_tuple(rec, BLOCK_COLS, injected)

    by_col = dict(zip(BLOCK_COLS, row))
    assert by_col["ingest_id"] == "ig1"
    assert by_col["sensitivity"] == "private"
    assert by_col["ingested_by"] == "alice@bayone.com"
    # Existing document-author `owner` is untouched by the ingester stamp.
    assert by_col["owner"] == "Doc Author"
    assert by_col["text_content"] == "hello"


def test_row_tuple_ignores_record_sensitivity() -> None:
    # A malicious/garbage payload cannot smuggle its own sensitivity/ingested_by:
    # injected wins because those columns are keyed in `injected`.
    injected = {"ingest_id": "ig1", "sensitivity": "internal", "ingested_by": None}
    rec = {"sensitivity": "private", "ingested_by": "attacker@evil.com"}
    by_col = dict(zip(BLOCK_COLS, _row_tuple(rec, BLOCK_COLS, injected)))
    assert by_col["sensitivity"] == "internal"
    assert by_col["ingested_by"] is None


# ── write() threads the stamp onto every row ─────────────────────────────────
class _FakeConn:
    def __init__(self) -> None:
        self.calls: list[tuple[str, list[tuple]]] = []

    def executemany(self, sql: str, rows: list[tuple]) -> int:
        self.calls.append((sql, rows))
        return len(rows)


def test_write_stamps_private_owner_on_all_rows() -> None:
    conn = _FakeConn()
    structured = SimpleNamespace(
        blocks=[{"block_index": 0, "text_content": "a"}, {"block_index": 1, "text_content": "b"}],
        facts=[{"fact_id": "f1", "attribute": "revenue"}],
    )
    blocks_written, facts_written = write(
        conn, structured, "ig9", sensitivity="private", ingested_by="alice@bayone.com"
    )
    assert (blocks_written, facts_written) == (2, 1)

    s_idx = BLOCK_COLS.index("sensitivity")
    o_idx = BLOCK_COLS.index("ingested_by")
    block_rows = conn.calls[0][1]
    assert all(r[s_idx] == "private" for r in block_rows)
    assert all(r[o_idx] == "alice@bayone.com" for r in block_rows)


def test_write_defaults_to_internal_unowned() -> None:
    conn = _FakeConn()
    structured = SimpleNamespace(blocks=[{"block_index": 0}], facts=[])
    write(conn, structured, "ig0")
    row = conn.calls[0][1][0]
    by_col = dict(zip(BLOCK_COLS, row))
    assert by_col["sensitivity"] == "internal"
    assert by_col["ingested_by"] is None


# ── caller-identity resolution (header beats dev fallback) ───────────────────
def _resolve(headers: dict[str, str], dev: str | None) -> str | None:
    from sf_agent import web

    web.STATE.auth_config = AuthConfig(
        _env_file=None, enforce_ownership=True, dev_caller_identity=dev
    )
    req = SimpleNamespace(headers=headers)
    return web._caller_identity(req)  # type: ignore[arg-type]


def test_caller_identity_prefers_header() -> None:
    got = _resolve({"X-MS-CLIENT-PRINCIPAL-NAME": "real@bayone.com"}, dev="dev@bayone.com")
    assert got == "real@bayone.com"


def test_caller_identity_falls_back_to_dev() -> None:
    assert _resolve({}, dev="dev@bayone.com") == "dev@bayone.com"


def test_caller_identity_none_when_neither() -> None:
    assert _resolve({}, dev=None) is None


# ── AuthConfig defaults preserve pre-feature behavior ────────────────────────
def test_auth_config_defaults_are_off() -> None:
    cfg = AuthConfig(_env_file=None)
    assert cfg.enforce_ownership is False
    assert cfg.caller_session_var == "BAYI_CALLER"
    assert cfg.easy_auth_header == "X-MS-CLIENT-PRINCIPAL-NAME"
    assert cfg.dev_caller_identity is None
    # protected-tier defaults
    assert cfg.protected_group == "SG-BayI-Sensitive"
    assert cfg.groups_header == "X-MS-CLIENT-GROUPS"
    assert cfg.protected_session_var == "BAYI_PROTECTED"
    assert cfg.dev_caller_groups is None
    assert cfg.dev_group_list == []


def test_dev_group_list_parses_and_strips() -> None:
    cfg = AuthConfig(_env_file=None, dev_caller_groups=" SG-BayI-Sensitive , Other , ")
    assert cfg.dev_group_list == ["SG-BayI-Sensitive", "Other"]


# ── protected-group membership (header beats dev fallback) ───────────────────
def _protected(headers: dict[str, str], dev_groups: str | None, group: str = "SG-BayI-Sensitive") -> bool:
    from sf_agent import web

    web.STATE.auth_config = AuthConfig(
        _env_file=None, enforce_ownership=True, protected_group=group, dev_caller_groups=dev_groups
    )
    req = SimpleNamespace(headers=headers)
    return web._caller_is_protected(req)  # type: ignore[arg-type]


def test_protected_member_from_header() -> None:
    assert _protected({"X-MS-CLIENT-GROUPS": "Foo, SG-BayI-Sensitive ,Bar"}, dev_groups=None) is True


def test_not_protected_when_group_absent_from_header() -> None:
    # Header present but without the privileged group -> the dev fallback is NOT consulted,
    # so a signed-in non-member is correctly denied.
    assert _protected({"X-MS-CLIENT-GROUPS": "Foo,Bar"}, dev_groups="SG-BayI-Sensitive") is False


def test_protected_falls_back_to_dev_groups() -> None:
    assert _protected({}, dev_groups="SG-BayI-Sensitive,Other") is True


def test_not_protected_when_neither() -> None:
    assert _protected({}, dev_groups=None) is False


# ── bind_session rejects an unsafe session-variable name ─────────────────────
def test_bind_session_rejects_bad_var_name() -> None:
    # The var name is interpolated into SET (can't be bound), so an invalid identifier
    # must raise before any SQL runs — guarding against injection via a config typo.
    conn = SnowflakeConnection(SimpleNamespace())  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        conn.bind_session({"BAYI_CALLER; DROP TABLE x": "a@b.com"})


# ── the guard still blocks the SET the app uses for binding ──────────────────
@pytest.mark.parametrize("stmt", ["SET BAYI_CALLER = 'x'", "USE ROLE BAYI_ADMIN_READ"])
def test_guard_blocks_session_statements(stmt: str) -> None:
    # bind_session runs SET on a raw cursor, deliberately outside this guard; a
    # model-authored query going through run_sql still can't set the variable itself.
    with pytest.raises(SqlGuardError):
        assert_read_only(stmt)
