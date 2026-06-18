import pytest
from pydantic import ValidationError

from sf_agent.config import SnowflakeConfig
from sf_agent.connection import SnowflakeConnection
from sf_agent.tools.run_sql import RunSqlTool


@pytest.fixture(scope="session")
def sf_config() -> SnowflakeConfig:
    """Load config from env/.env, or skip the whole integration suite if absent."""
    try:
        return SnowflakeConfig()  # type: ignore[call-arg]
    except ValidationError as e:
        pytest.skip(f"Snowflake creds not configured; skipping integration tests ({e.error_count()} missing)")


@pytest.fixture(scope="session")
def connection(sf_config: SnowflakeConfig):
    with SnowflakeConnection(sf_config) as conn:
        yield conn


@pytest.fixture()
def run_sql(connection) -> RunSqlTool:
    return RunSqlTool(connection)
