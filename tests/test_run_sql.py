import pytest

pytestmark = pytest.mark.integration

TARGET_TABLE = "BILLABLE_DATA_BY_JOB_DESCRIPTION"


def test_count_is_grounded(run_sql) -> None:
    result = run_sql.run(f"SELECT COUNT(*) AS N FROM {TARGET_TABLE}")
    assert result.ok is True
    assert result.error is None
    assert result.result is not None
    assert result.result.row_count == 1
    assert result.executed_sql == f"SELECT COUNT(*) AS N FROM {TARGET_TABLE}"
    assert result.elapsed_ms > 0


def test_limit_returns_columns(run_sql) -> None:
    result = run_sql.run(f"SELECT * FROM {TARGET_TABLE} LIMIT 5")
    assert result.ok is True
    assert result.result is not None
    assert len(result.result.columns) > 0
    assert result.result.row_count <= 5


def test_max_rows_truncates(run_sql) -> None:
    result = run_sql.run(f"SELECT * FROM {TARGET_TABLE}", max_rows=2)
    assert result.ok is True
    assert result.result is not None
    assert result.result.row_count <= 2
    # truncated is True only if the table actually has more than 2 rows.


def test_guard_rejects_dml(run_sql) -> None:
    result = run_sql.run(f"DROP TABLE {TARGET_TABLE}")
    assert result.ok is False
    assert result.error is not None
    assert result.error.type == "guard_rejected"
    assert result.result is None


def test_guard_rejects_stacked_statements(run_sql) -> None:
    result = run_sql.run(f"SELECT 1; DROP TABLE {TARGET_TABLE}")
    assert result.ok is False
    assert result.error is not None
    assert result.error.type == "guard_rejected"
