import pytest

pytestmark = pytest.mark.integration


def test_select_one(connection) -> None:
    result = connection.execute("SELECT 1 AS ONE")
    assert result.columns == ["ONE"]
    assert result.row_count == 1
    assert result.rows[0][0] == 1
    assert result.truncated is False


def test_row_cap_truncation(connection) -> None:
    # Three rows requested, cap of 2 -> truncated with exactly 2 returned.
    result = connection.execute(
        "SELECT * FROM (VALUES (1), (2), (3)) AS v(n) ORDER BY n", max_rows=2
    )
    assert result.row_count == 2
    assert result.truncated is True
