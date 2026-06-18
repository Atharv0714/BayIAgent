import pytest

from sf_agent.sql_guard import SqlGuardError, assert_read_only


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT 1",
        "select count(*) from billable_data_by_job_description",
        "SELECT * FROM t WHERE name = 'DELETE FROM x'",  # DML only inside a string literal
        "WITH c AS (SELECT 1 AS a) SELECT a FROM c",
        "SELECT col_set, budget, offset_amt FROM t",  # words containing forbidden substrings
        "SELECT replace(name, 'a', 'b') FROM t",  # REPLACE is an allowed scalar function
        "  SELECT 1 ;  ",  # trailing semicolon + whitespace is fine
        "/* a comment */ SELECT 1",
    ],
)
def test_allows_read_only(sql: str) -> None:
    assert_read_only(sql)  # should not raise


@pytest.mark.parametrize(
    "sql",
    [
        "DROP TABLE t",
        "DELETE FROM t",
        "UPDATE t SET x = 1",
        "INSERT INTO t VALUES (1)",
        "MERGE INTO t USING s ON t.id = s.id WHEN MATCHED THEN UPDATE SET x = 1",
        "SELECT 1; DROP TABLE t",  # stacked statement
        "WITH c AS (SELECT 1) DELETE FROM t",  # DML hidden behind a CTE
        "/* SELECT */ DROP TABLE t",  # DML hidden behind a comment prefix
        "GRANT SELECT ON t TO ROLE r",
        "USE WAREHOUSE wh",
        "CALL my_proc()",
        "TRUNCATE TABLE t",
        "",
        "   ",
    ],
)
def test_rejects_non_read_only(sql: str) -> None:
    with pytest.raises(SqlGuardError):
        assert_read_only(sql)
