from __future__ import annotations

import logging
from pathlib import Path

import snowflake.connector
from cryptography.hazmat.primitives import serialization

from sf_agent.config import SnowflakeConfig
from sf_agent.types import QueryResult

logger = logging.getLogger("sf_agent.connection")

# Snowflake error codes that mean "the session is gone, open a new one":
# expired auth token, and session-no-longer-exists.
_EXPIRED_SESSION_ERRNOS = {390114, 390104, 390108, 390195}


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
    """Snowflake connection (password or key-pair) with a thin `execute` that
    returns QueryResult.

    Use as a context manager:

        with SnowflakeConnection(config) as conn:
            result = conn.execute("SELECT 1")
    """

    def __init__(self, config: SnowflakeConfig) -> None:
        self._config = config
        self._conn: snowflake.connector.SnowflakeConnection | None = None

    def connect(self) -> "SnowflakeConnection":
        cfg = self._config
        auth: dict[str, object]
        if cfg.password:
            auth = {"password": cfg.password}
        else:
            assert cfg.private_key_path is not None  # guaranteed by config validation
            auth = {
                "private_key": _load_private_key(
                    cfg.private_key_path, cfg.private_key_passphrase or None
                )
            }
        self._conn = snowflake.connector.connect(
            account=cfg.account,
            user=cfg.user,
            warehouse=cfg.warehouse,
            database=cfg.database,
            schema=cfg.schema_name,
            role=cfg.role,
            # Long-running server: heartbeat the session so its auth token is renewed
            # instead of expiring while idle (Snowflake error 390114).
            client_session_keep_alive=True,
            **auth,
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

        Fetches one extra row past the cap to detect (and flag) truncation. If the
        Snowflake session has expired, reconnects once and retries so a stale token
        doesn't surface as a hard error to the caller.
        """
        if self._conn is None:
            raise RuntimeError("connection is not open; call connect() or use as context manager")

        cap = max_rows if max_rows is not None else self._config.row_cap
        try:
            return self._run(sql, cap)
        except snowflake.connector.errors.Error as e:
            if getattr(e, "errno", None) not in _EXPIRED_SESSION_ERRNOS:
                raise
            logger.warning("snowflake session expired (errno=%s); reconnecting and retrying", e.errno)
            self.close()
            self.connect()
            return self._run(sql, cap)

    def execute_ddl(self, sql: str) -> None:
        """Run a statement that returns no rows (e.g. CREATE TABLE) and commit.

        Deliberately separate from `execute`, which assumes a result set. Used only
        by the ingest write path — never by the read agent.
        """
        if self._conn is None:
            raise RuntimeError("connection is not open; call connect() or use as context manager")
        cur = self._conn.cursor()
        try:
            cur.execute(sql)
            self._conn.commit()
        finally:
            cur.close()

    def executemany(self, sql: str, rows: list[tuple[object, ...]]) -> int:
        """Bulk-insert `rows` via a parametrized statement and commit; return the count.

        The SQL is app-authored with bound value placeholders (never model text), so
        it bypasses the read-only guard by design. A no-op when `rows` is empty.
        """
        if self._conn is None:
            raise RuntimeError("connection is not open; call connect() or use as context manager")
        if not rows:
            return 0
        cur = self._conn.cursor()
        try:
            cur.executemany(sql, rows)
            self._conn.commit()
            return len(rows)
        finally:
            cur.close()

    def _run(self, sql: str, cap: int) -> QueryResult:
        assert self._conn is not None
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
