import re


class SqlGuardError(ValueError):
    """Raised when a statement is not a safe, single read-only query."""


_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)
_LINE_COMMENT = re.compile(r"--[^\n]*")
_SINGLE_QUOTE = re.compile(r"'(?:''|[^'])*'")
_DOLLAR_QUOTE = re.compile(r"\$\$.*?\$\$", re.DOTALL)

# Verbs that write, change state, or run side effects. A read-only query never
# contains these as statement keywords — including inside a CTE (e.g. a WITH that
# fronts a DELETE) which a naive "starts with SELECT/WITH" check would miss.
# REPLACE is intentionally absent: it's a common scalar function, and CREATE OR
# REPLACE is already caught by CREATE.
_FORBIDDEN = {
    "INSERT", "UPDATE", "DELETE", "MERGE", "UPSERT",
    "CREATE", "DROP", "ALTER", "TRUNCATE", "RENAME", "UNDROP",
    "GRANT", "REVOKE", "USE", "SET", "UNSET",
    "CALL", "EXECUTE", "COPY", "PUT", "GET", "REMOVE",
    "BEGIN", "COMMIT", "ROLLBACK", "COMMENT",
}
_WORD = re.compile(r"[A-Za-z_][A-Za-z_0-9$]*")


def _strip(sql: str) -> str:
    """Remove comments and string/dollar-quoted literals so keyword scanning sees
    only executable tokens (this defeats DML hidden inside comments or strings)."""
    sql = _BLOCK_COMMENT.sub(" ", sql)
    sql = _LINE_COMMENT.sub(" ", sql)
    sql = _DOLLAR_QUOTE.sub(" ", sql)
    sql = _SINGLE_QUOTE.sub("''", sql)
    return sql


def assert_read_only(sql: str) -> None:
    """Validate that `sql` is a single read-only statement. Raises SqlGuardError.

    This is a secondary defense; the primary boundary is a read-only Snowflake role.
    """
    if sql is None or not sql.strip():
        raise SqlGuardError("empty query")

    stripped = _strip(sql).strip()

    # No stacked statements: at most one non-empty segment between semicolons.
    segments = [s for s in stripped.split(";") if s.strip()]
    if len(segments) > 1:
        raise SqlGuardError("multiple statements are not allowed")
    if not segments:
        raise SqlGuardError("empty query")

    statement = segments[0]
    tokens = [t.upper() for t in _WORD.findall(statement)]
    if not tokens:
        raise SqlGuardError("no SQL keyword found")

    if tokens[0] not in {"SELECT", "WITH"}:
        raise SqlGuardError(f"only SELECT/WITH queries are allowed, got '{tokens[0]}'")

    forbidden = _FORBIDDEN.intersection(tokens)
    if forbidden:
        raise SqlGuardError(f"forbidden keyword(s): {', '.join(sorted(forbidden))}")
