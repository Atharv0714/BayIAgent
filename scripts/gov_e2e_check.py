#!/usr/bin/env python
"""End-to-end proof that rap_ownership isolates a private row between two identities.

Exercises the REAL enforcement boundary on the production ``blocks``/``facts`` tables,
after ``rap_ownership`` is attached:

  1. Read-only: as ``BAYI_READ`` with no identity bound, confirm only ``internal`` rows are
     visible (fail-closed) — the check the runbook describes.
  2. Insert ONE clearly-labelled private block + fact owned by ``alice@bayone.com`` through
     the app's own write path (``ingest_store.write`` as ``BAYI_INGEST_WRITE``).
  3. Through the policy, on the app's ``BAYI_READ`` read connection: ``alice`` sees the row,
     ``bob`` does NOT, and an unbound caller does NOT.
  4. Delete the test rows (owner role) in a ``finally`` — nothing is left in production.

The row-access policy makes the private row invisible to any SELECT ``bob`` can issue, so no
question ``bob`` asks the agent can surface it — the agent only ever sees what SELECT returns.

Run from the repo root:  .venv/bin/python scripts/gov_e2e_check.py
Exit 0 = isolation verified. Non-zero = a leak or setup problem (message says which).
"""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "src"))

from sf_agent.config import SnowflakeConfig, SnowflakeIngestConfig  # noqa: E402
from sf_agent.connection import SnowflakeConnection  # noqa: E402
from sf_agent.ingest import StructuredResult  # noqa: E402
from sf_agent.ingest_store import write as ingest_write  # noqa: E402

_ENV = _ROOT / ".env"
_INGEST_ID = "gov-e2e-probe"  # distinctive tag; every test row carries it and is deleted
_MARKER = "GOVE2E-SECRET-ALICE"
_ALICE = "alice@bayone.com"
_BOB = "bob@bayone.com"


def _count_marker(read: SnowflakeConnection) -> int:
    """Rows the current BAYI_READ session can see for our test tag (blocks + facts)."""
    b = read.execute(
        f"SELECT COUNT(*) FROM blocks WHERE ingest_id = '{_INGEST_ID}' AND text_content = '{_MARKER}'"
    ).rows[0][0]
    f = read.execute(
        f"SELECT COUNT(*) FROM facts WHERE ingest_id = '{_INGEST_ID}' AND raw_value = '{_MARKER}'"
    ).rows[0][0]
    return int(b) + int(f)


def main() -> int:
    base = SnowflakeConfig(_env_file=str(_ENV))  # type: ignore[call-arg]
    read_cfg = base.model_copy(update={"role": "BAYI_READ"})
    # Write path as the least-privilege ingest owner role (matches production posture).
    ingest_cfg = SnowflakeIngestConfig(_env_file=str(_ENV)).model_copy(  # type: ignore[call-arg]
        update={"role": "BAYI_INGEST_WRITE"}
    )

    read = SnowflakeConnection(read_cfg).connect()
    ingest = SnowflakeConnection(ingest_cfg).connect()
    failures: list[str] = []

    try:
        # 1. Read-only fail-closed baseline: unbound caller sees internal rows only.
        read.bind_session({"BAYI_CALLER": None, "BAYI_PROTECTED": "false"})
        by_tier = read.execute(
            "SELECT sensitivity, COUNT(*) FROM blocks GROUP BY sensitivity ORDER BY 1"
        ).rows
        print(f"1. unbound BAYI_READ sees blocks by tier: {[tuple(r) for r in by_tier]}")
        noninternal = [tuple(r) for r in by_tier if r[0] not in (None, "internal")]
        if noninternal:
            print(f"   FAIL: unbound caller sees non-internal rows {noninternal}")
            failures.append("unbound baseline")

        # 2. Ingest one private block + fact owned by alice, via the app write path.
        sr = StructuredResult(
            manifest={},
            blocks=[{"block_index": 1, "section_title": "gov-e2e", "text_content": _MARKER,
                     "sensitivity": "private"}],
            facts=[{"fact_id": "gov-e2e-fact-1", "entity_type": "gov-e2e", "entity_name": "alice",
                    "attribute": "marker", "raw_value": _MARKER, "sensitivity": "private"}],
            warnings=[], errors=[], elapsed_ms=0.0, tokens={}, cost_usd=0.0,
        )
        nb, nf = ingest_write(ingest, sr, _INGEST_ID, sensitivity="private", ingested_by=_ALICE)
        print(f"2. wrote private test rows as alice: blocks={nb} facts={nf}")

        # 3. Isolation through the policy on ONE read connection, re-binding the caller.
        read.bind_session({"BAYI_CALLER": _ALICE})
        seen_alice = _count_marker(read)
        print(f"3a. caller=alice sees marker rows: {seen_alice} (expect 2)")
        if seen_alice != 2:
            failures.append("alice cannot see her own private rows")

        read.bind_session({"BAYI_CALLER": _BOB})
        seen_bob = _count_marker(read)
        print(f"3b. caller=bob sees marker rows:   {seen_bob} (expect 0)")
        if seen_bob != 0:
            failures.append("LEAK: bob sees alice's private rows")

        read.bind_session({"BAYI_CALLER": None})
        seen_none = _count_marker(read)
        print(f"3c. unbound sees marker rows:       {seen_none} (expect 0)")
        if seen_none != 0:
            failures.append("LEAK: unbound caller sees private rows")

    finally:
        # 4. Delete the test rows as the owner role, always. The owner (BAYI_INGEST_WRITE)
        # is NOT in the policy's admin branch, so it must bind the owner identity first —
        # otherwise the policy hides alice's private rows from the DELETE and it removes 0.
        try:
            ingest.bind_session({"BAYI_CALLER": _ALICE, "BAYI_PROTECTED": "false"})
            nb = ingest.execute(f"DELETE FROM blocks WHERE ingest_id = '{_INGEST_ID}'").rows
            nf = ingest.execute(f"DELETE FROM facts  WHERE ingest_id = '{_INGEST_ID}'").rows
            print(f"4. cleaned up test rows (blocks deleted={nb[0][0]}, facts deleted={nf[0][0]})")
        except Exception as e:  # noqa: BLE001
            print(f"4. WARNING: cleanup failed, remove ingest_id={_INGEST_ID!r} manually: {e}")
        read.close()
        ingest.close()

    print()
    if failures:
        print("RESULT: FAIL — " + "; ".join(failures))
        return 1
    print("RESULT: PASS — private rows are isolated: alice sees them, bob and unbound do not.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
