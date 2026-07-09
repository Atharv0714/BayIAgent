"""Apply a .sql file to Snowflake statement-by-statement via SnowflakeConnection.

The connector executes one statement per call, so we strip `--` comments and split
on `;`. CREATE SEMANTIC VIEW / CREATE VIEW / GRANT have no internal semicolons, so a
naive split is safe here.

Usage: python scripts/apply_sql.py sql/billable_semantic_view.sql
"""

from __future__ import annotations

import sys
from pathlib import Path

from sf_agent.config import SnowflakeConfig
from sf_agent.connection import SnowflakeConnection


def split_statements(text: str) -> list[str]:
    lines = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("--"):
            continue
        lines.append(line)
    body = "\n".join(lines)
    return [s.strip() for s in body.split(";") if s.strip()]


def main() -> None:
    path = Path(sys.argv[1])
    statements = split_statements(path.read_text())
    cfg = SnowflakeConfig()
    with SnowflakeConnection(cfg) as conn:
        for i, stmt in enumerate(statements, 1):
            head = " ".join(stmt.split())[:80]
            print(f"[{i}/{len(statements)}] {head} ...")
            try:
                conn.execute(stmt)
                print("    OK")
            except Exception as e:  # noqa: BLE001
                print(f"    FAILED: {e}")
                raise


if __name__ == "__main__":
    main()
