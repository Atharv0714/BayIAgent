#!/usr/bin/env python
"""Deciding probe: is session-variable row filtering safe on this app's connection model?

The row-access policy (docs/sql/03_row_access_policy.sql) filters private/protected rows
with ``getvariable('BAYI_CALLER')`` / ``getvariable('BAYI_PROTECTED')``. The app holds ONE
Snowflake session shared by every caller and re-runs ``SET BAYI_CALLER`` per request.

Snowflake's GETVARIABLE docs warn it "uses the result cache for the current session ...
including the body of policy objects, such as a row access policy." If that cache does not
invalidate on a re-``SET``, the policy evaluates a STALE caller and serves one user another
user's private rows — silently.

This settles it empirically, on the app's own ``SnowflakeConnection`` class, entirely on ONE
``BAYI_READ`` session (exactly how the app behaves). It touches a scratch table only; it never
reads or writes ``blocks``/``facts``.

Run from the repo root (loads ./.env):

    .venv/bin/python scripts/gov_session_probe.py

Exit 0 = SAFE (getvariable tracked each SET). Exit non-zero = LEAK; do not attach the policy
to blocks/facts until the app uses a session per caller.
"""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "src"))

from sf_agent.config import SnowflakeConfig  # noqa: E402
from sf_agent.connection import SnowflakeConnection  # noqa: E402

_ENV = _ROOT / ".env"

# Byte-identical to the policy body in docs/sql/03_row_access_policy.sql, so the probe
# tests the real thing. current_role() is BAYI_READ here, so the admin branch is never
# what makes a row visible — a pass is a true pass, not the break-glass bypass.
_POLICY_BODY = (
    "sensitivity = 'internal' "
    "OR (sensitivity = 'private' AND ingested_by = getvariable('BAYI_CALLER')) "
    "OR (sensitivity = 'protected' AND getvariable('BAYI_PROTECTED') = 'true') "
    "OR current_role() IN ('BAYI_ADMIN_READ', 'ACCOUNTADMIN')"
)

_SEED = (
    "INSERT INTO gov_probe (id, label, sensitivity, ingested_by) VALUES "
    "(1, 'shared',    'internal',  NULL), "
    "(2, 'alice',     'private',   'alice@bayone.com'), "
    "(3, 'bob',       'private',   'bob@bayone.com'), "
    "(4, 'protected', 'protected', NULL)"
)


def _visible_labels(conn: SnowflakeConnection) -> list[str]:
    """The labels the BAYI_READ session can currently see through the policy."""
    result = conn.execute("SELECT id, label FROM gov_probe ORDER BY id")
    return sorted(str(row[1]) for row in result.rows)


def main() -> int:
    base = SnowflakeConfig(_env_file=str(_ENV))  # type: ignore[call-arg]
    # Setup/teardown need ACCOUNTADMIN (DDL + policy + grant). Reads run as the app's
    # least-privilege role so the policy actually bites (ACCOUNTADMIN would pass vacuously).
    setup_cfg = base.model_copy(update={"role": "ACCOUNTADMIN"})
    read_cfg = base.model_copy(update={"role": "BAYI_READ"})

    setup = SnowflakeConnection(setup_cfg).connect()
    read: SnowflakeConnection | None = None
    failures: list[str] = []

    try:
        setup.execute_ddl(
            "CREATE OR REPLACE TABLE gov_probe "
            "(id NUMBER, label VARCHAR, sensitivity VARCHAR, ingested_by VARCHAR)"
        )
        setup.execute_ddl(_SEED)
        setup.execute_ddl(
            "CREATE OR REPLACE ROW ACCESS POLICY rap_gov_probe "
            "AS (sensitivity VARCHAR, ingested_by VARCHAR) RETURNS BOOLEAN -> " + _POLICY_BODY
        )
        setup.execute_ddl(
            "ALTER TABLE gov_probe ADD ROW ACCESS POLICY rap_gov_probe ON (sensitivity, ingested_by)"
        )
        setup.execute_ddl("GRANT SELECT ON TABLE gov_probe TO ROLE BAYI_READ")

        # Every read below is on THIS one connection — one session, re-SET per "caller".
        read = SnowflakeConnection(read_cfg).connect()

        def check(step: str, expected: list[str]) -> None:
            got = _visible_labels(read)  # type: ignore[arg-type]
            ok = got == sorted(expected)
            print(f"[{'PASS' if ok else 'FAIL'}] {step}: visible={got} expected={sorted(expected)}")
            if not ok:
                failures.append(step)

        read.bind_session({"BAYI_CALLER": "alice@bayone.com", "BAYI_PROTECTED": "false"})
        check("1. caller=alice", ["shared", "alice"])

        # THE decisive step: same session, next caller. If 'alice' still shows, getvariable
        # served a cached value and the shared-session design leaks across users.
        read.bind_session({"BAYI_CALLER": "bob@bayone.com"})
        check("2. caller=bob (DECISIVE — must NOT show alice)", ["shared", "bob"])

        read.bind_session({"BAYI_PROTECTED": "true"})
        check("3. caller=bob, protected=true", ["shared", "bob", "protected"])

        read.bind_session({"BAYI_PROTECTED": "false"})
        check("4. caller=bob, protected=false (protected must vanish)", ["shared", "bob"])

        read.bind_session({"BAYI_CALLER": None})  # UNSET
        check("5. no identity (UNSET) — fail closed to shared only", ["shared"])

    finally:
        for ddl in (
            "ALTER TABLE gov_probe DROP ROW ACCESS POLICY rap_gov_probe",
            "DROP ROW ACCESS POLICY IF EXISTS rap_gov_probe",
            "DROP TABLE IF EXISTS gov_probe",
        ):
            try:
                setup.execute_ddl(ddl)
            except Exception as e:  # noqa: BLE001 — teardown is best-effort
                print(f"  (teardown warning: {ddl!r} -> {e})")
        if read is not None:
            read.close()
        setup.close()

    print()
    if failures:
        print("RESULT: UNSAFE / LEAK — failed steps: " + ", ".join(failures))
        print(
            "getvariable() did NOT track the re-SET on the shared session: the policy served a "
            "stale caller. Do NOT attach rap_ownership to blocks/facts until the app uses a "
            "session per caller (Task 2)."
        )
        return 1
    print("RESULT: SAFE — getvariable() tracked every SET on the single shared session.")
    print(
        "Per-caller filtering is correct on one connection; the shared-session model is sound "
        "as written. No session redesign required (Task 2 can be skipped)."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
