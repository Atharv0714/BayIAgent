"""Integration: the row-access policy isolates callers on ONE shared session.

This is the regression guard for the decision settled by scripts/gov_session_probe.py —
that getvariable('BAYI_CALLER') in a policy body tracks a re-SET on a single Snowflake
session. It runs entirely on a scratch table (never touches blocks/facts) and, like the
other live tests, is marked `integration` so it skips cleanly without credentials or when
the BAYI_READ role is absent.

If getvariable caching ever regressed (or someone reintroduced a single global session
that didn't re-bind), assertion 2 below — Bob must not see Alice's private row on the
session that just served Alice — would fail.
"""

from __future__ import annotations

import pytest

from sf_agent.config import SnowflakeConfig
from sf_agent.connection import SnowflakeConnection

pytestmark = pytest.mark.integration

_POLICY_BODY = (
    "sensitivity = 'internal' "
    "OR (sensitivity = 'private' AND ingested_by = getvariable('BAYI_CALLER')) "
    "OR (sensitivity = 'protected' AND getvariable('BAYI_PROTECTED') = 'true') "
    "OR current_role() IN ('BAYI_ADMIN_READ', 'ACCOUNTADMIN')"
)
_TABLE = "gov_isolation_test"
_POLICY = "rap_gov_isolation_test"


def _labels(conn: SnowflakeConnection) -> set[str]:
    return {str(r[1]) for r in conn.execute(f"SELECT id, label FROM {_TABLE} ORDER BY id").rows}


@pytest.fixture()
def read_conn():
    """ACCOUNTADMIN sets up a scratch table + policy; yield a single BAYI_READ connection.

    Skips (never fails) when creds are absent or the BAYI_READ role has not been created,
    so the suite stays green on a machine without the live warehouse."""
    try:
        base = SnowflakeConfig()  # type: ignore[call-arg]  # loads .env
    except Exception as e:  # noqa: BLE001
        pytest.skip(f"no Snowflake config: {e}")

    try:
        setup = SnowflakeConnection(base.model_copy(update={"role": "ACCOUNTADMIN"})).connect()
    except Exception as e:  # noqa: BLE001
        pytest.skip(f"cannot connect to Snowflake as ACCOUNTADMIN: {e}")

    read: SnowflakeConnection | None = None
    try:
        setup.execute_ddl(
            f"CREATE OR REPLACE TABLE {_TABLE} "
            "(id NUMBER, label VARCHAR, sensitivity VARCHAR, ingested_by VARCHAR)"
        )
        setup.execute_ddl(
            f"INSERT INTO {_TABLE} (id, label, sensitivity, ingested_by) VALUES "
            "(1,'shared','internal',NULL),(2,'alice','private','alice@bayone.com'),"
            "(3,'bob','private','bob@bayone.com'),(4,'protected','protected',NULL)"
        )
        setup.execute_ddl(
            f"CREATE OR REPLACE ROW ACCESS POLICY {_POLICY} "
            "AS (sensitivity VARCHAR, ingested_by VARCHAR) RETURNS BOOLEAN -> " + _POLICY_BODY
        )
        setup.execute_ddl(f"ALTER TABLE {_TABLE} ADD ROW ACCESS POLICY {_POLICY} ON (sensitivity, ingested_by)")
        try:
            setup.execute_ddl(f"GRANT SELECT ON TABLE {_TABLE} TO ROLE BAYI_READ")
            read = SnowflakeConnection(base.model_copy(update={"role": "BAYI_READ"})).connect()
        except Exception as e:  # noqa: BLE001
            pytest.skip(f"BAYI_READ role unavailable (run deploy/snowflake-roles.sql): {e}")
        yield read
    finally:
        for ddl in (
            f"ALTER TABLE {_TABLE} DROP ROW ACCESS POLICY {_POLICY}",
            f"DROP ROW ACCESS POLICY IF EXISTS {_POLICY}",
            f"DROP TABLE IF EXISTS {_TABLE}",
        ):
            try:
                setup.execute_ddl(ddl)
            except Exception:  # noqa: BLE001
                pass
        if read is not None:
            read.close()
        setup.close()


def test_caller_isolation_on_one_session(read_conn):
    # 1. Alice sees shared + her own private row.
    read_conn.bind_session({"BAYI_CALLER": "alice@bayone.com", "BAYI_PROTECTED": "false"})
    assert _labels(read_conn) == {"shared", "alice"}

    # 2. DECISIVE: same session, re-SET to Bob. Alice's row must be gone.
    read_conn.bind_session({"BAYI_CALLER": "bob@bayone.com"})
    assert _labels(read_conn) == {"shared", "bob"}, "leak: Bob saw Alice's private row (stale getvariable)"

    # 3. Protected flag toggles cleanly on the same session.
    read_conn.bind_session({"BAYI_PROTECTED": "true"})
    assert _labels(read_conn) == {"shared", "bob", "protected"}
    read_conn.bind_session({"BAYI_PROTECTED": "false"})
    assert _labels(read_conn) == {"shared", "bob"}

    # 4. Unbound caller fails closed to shared only.
    read_conn.bind_session({"BAYI_CALLER": None})
    assert _labels(read_conn) == {"shared"}
