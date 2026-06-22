"""Helper to print the real columns of the target table, so the eval fixtures'
column constants can be confirmed. Run with:

    uv run pytest -k print_schema -s -m integration
"""

import pytest

from eval_fixtures import TABLE

pytestmark = pytest.mark.integration


def test_print_schema(connection) -> None:
    result = connection.execute(f"SELECT * FROM {TABLE} LIMIT 1")
    print(f"\n{TABLE} columns ({len(result.columns)}):")
    for col in result.columns:
        print(f"  - {col}")
    assert result.columns, "table appears to have no columns"
