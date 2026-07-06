import pytest
from pydantic import ValidationError

from sf_agent.agent import SnowflakeAgent
from sf_agent.config import AgentConfig, CortexConfig, SnowflakeConfig
from sf_agent.connection import SnowflakeConnection
from sf_agent.tools.cortex_analyst import CortexAnalystTool
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


@pytest.fixture(scope="session")
def agent_config() -> AgentConfig:
    """Agent config (needs ANTHROPIC_API_KEY); skip the eval suite if absent."""
    try:
        return AgentConfig()  # type: ignore[call-arg]
    except ValidationError:
        pytest.skip("ANTHROPIC_API_KEY not configured; skipping agent eval tests")


@pytest.fixture()
def agent(connection, agent_config: AgentConfig) -> SnowflakeAgent:
    return SnowflakeAgent(tools=[RunSqlTool(connection)], config=agent_config)


@pytest.fixture(scope="session")
def cortex_config() -> CortexConfig:
    """Cortex Analyst config (needs SNOWFLAKE_PAT + CORTEX_SEMANTIC_VIEW); skip if absent."""
    try:
        return CortexConfig()  # type: ignore[call-arg]
    except ValidationError:
        pytest.skip("SNOWFLAKE_PAT / CORTEX_SEMANTIC_VIEW not configured; skipping Cortex tests")


@pytest.fixture()
def cortex_agent(connection, agent_config: AgentConfig, cortex_config: CortexConfig) -> SnowflakeAgent:
    """An agent whose only tool is Cortex Analyst — proves the loop is tool-agnostic."""
    return SnowflakeAgent(
        tools=[CortexAnalystTool(connection, cortex_config)], config=agent_config
    )
