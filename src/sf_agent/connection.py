from __future__ import annotations

from pathlib import Path

import snowflake.connector
from cryptography.hazmat.primitives import serialization

from sf_agent.config import SnowflakeConfig
from sf_agent.types import QueryResult


def _load_private_key(path: str, passphrase: str | None) -> bytes:
    """Load a PEM private key and return PKCS8 DER bytes, the form the Snowflake
    connector's `private_key` parameter expects."""
    pem = Path(path).read_bytes()
    key = serialization.load_pem_private_key(
        pem,
        password=passphrase.encode() if passphrase else None,
    )
    return key.private_bytes(
        encoding=serialization.Encoding.DER,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )


class SnowflakeConnection:
    """Key-pair Snowflake connection with a thin `execute` that returns QueryResult.

    Use as a context manager:

        with SnowflakeConnection(config) as conn:
            result = conn.execute("SELECT 1")
    """

    def __init__(self, config: SnowflakeConfig) -> None:
        self._config = config
        self._conn: snowflake.connector.SnowflakeConnection | None = None

    def connect(self) -> "SnowflakeConnection":
        cfg = self._config
        pkb = _load_private_key(cfg.private_key_path, cfg.private_key_passphrase or None)
        self._conn = snowflake.connector.connect(
            account=cfg.account,
            user=cfg.user,
            private_key=pkb,
            warehouse=cfg.warehouse,
            database=cfg.database,
            schema=cfg.schema_name,
            role=cfg.role,
        )
        return self

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def __enter__(self) -> "SnowflakeConnection":
        return self.connect()

    def __exit__(self, *exc: object) -> None:
        self.close()

    def execute(self, sql: str, max_rows: int | None = None) -> QueryResult:
        """Run `sql` and return up to `max_rows` (default: config.row_cap) rows.

        Fetches one extra row past the cap to detect (and flag) truncation.
        """
        if self._conn is None:
            raise RuntimeError("connection is not open; call connect() or use as context manager")

        cap = max_rows if max_rows is not None else self._config.row_cap
        cur = self._conn.cursor()
        try:
            cur.execute(sql)
            columns = [c[0] for c in cur.description] if cur.description else []
            fetched = cur.fetchmany(cap + 1)
            truncated = len(fetched) > cap
            rows = [list(r) for r in fetched[:cap]]
            return QueryResult(
                columns=columns,
                rows=rows,
                row_count=len(rows),
                truncated=truncated,
            )
        finally:
            cur.close()
