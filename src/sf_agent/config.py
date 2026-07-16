from pydantic import AliasChoices, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class SnowflakeConfig(BaseSettings):
    """Typed Snowflake connection config, loaded from env / .env.

    Exactly one auth method must be supplied: either a password
    (SNOWFLAKE_PASSWORD) or a key-pair (SNOWFLAKE_PRIVATE_KEY_PATH, plus an
    optional passphrase). Blank values count as unset.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
    )

    account: str = Field(min_length=1, validation_alias="SNOWFLAKE_ACCOUNT")
    user: str = Field(min_length=1, validation_alias="SNOWFLAKE_USER")
    password: str | None = Field(default=None, validation_alias="SNOWFLAKE_PASSWORD")
    private_key_path: str | None = Field(
        default=None, validation_alias="SNOWFLAKE_PRIVATE_KEY_PATH"
    )
    private_key_passphrase: str | None = Field(
        default=None, validation_alias="SNOWFLAKE_PRIVATE_KEY_PASSPHRASE"
    )
    warehouse: str = Field(min_length=1, validation_alias="SNOWFLAKE_WAREHOUSE")
    database: str = Field(min_length=1, validation_alias="SNOWFLAKE_DATABASE")
    schema_name: str = Field(min_length=1, validation_alias="SNOWFLAKE_SCHEMA")
    role: str = Field(min_length=1, validation_alias="SNOWFLAKE_ROLE")

    # Rows fed back to the model per query. Kept modest because every fetched row
    # is serialized into the tool result and re-sent across the loop's rounds, so a
    # large cap multiplies token cost; 200 still covers realistic full-list answers.
    row_cap: int = Field(default=200, gt=0, validation_alias="SF_ROW_CAP")

    @field_validator("password", "private_key_path", "private_key_passphrase", mode="before")
    @classmethod
    def _blank_to_none(cls, v: object) -> object:
        if isinstance(v, str) and v.strip() == "":
            return None
        return v

    @model_validator(mode="after")
    def _exactly_one_auth_method(self) -> "SnowflakeConfig":
        if bool(self.password) == bool(self.private_key_path):
            raise ValueError(
                "set exactly one of SNOWFLAKE_PASSWORD or SNOWFLAKE_PRIVATE_KEY_PATH"
            )
        return self


class SnowflakeIngestConfig(SnowflakeConfig):
    """Write-capable Snowflake config for the ingest load path.

    Shares SNOWFLAKE_ACCOUNT / SNOWFLAKE_USER and the same auth method as the
    read connection (inherited), but points at its own role / database / schema
    (and optionally warehouse) via SNOWFLAKE_INGEST_* so the query connection can
    stay strictly read-only. The ingest role should be the only write-capable one.
    """

    role: str = Field(min_length=1, validation_alias="SNOWFLAKE_INGEST_ROLE")
    database: str = Field(min_length=1, validation_alias="SNOWFLAKE_INGEST_DATABASE")
    schema_name: str = Field(min_length=1, validation_alias="SNOWFLAKE_INGEST_SCHEMA")
    # Warehouse falls back to the read warehouse when SNOWFLAKE_INGEST_WAREHOUSE is
    # unset, so a separate compute pool for writes is optional.
    warehouse: str = Field(
        min_length=1,
        validation_alias=AliasChoices("SNOWFLAKE_INGEST_WAREHOUSE", "SNOWFLAKE_WAREHOUSE"),
    )


class AgentConfig(BaseSettings):
    """Config for the Anthropic tool-use agent loop, loaded from env / .env."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
    )

    api_key: str = Field(validation_alias="ANTHROPIC_API_KEY")
    model: str = Field(default="claude-sonnet-4-6", validation_alias="AGENT_MODEL")
    max_rounds: int = Field(default=8, gt=0, validation_alias="AGENT_MAX_ROUNDS")
    max_tokens: int = Field(default=8192, gt=0, validation_alias="AGENT_MAX_TOKENS")
    # Cap on how many searches the web-route path may run per question (SDK web_search
    # tool). Keeps a single internet answer bounded in latency and cost.
    web_search_max_uses: int = Field(default=5, gt=0, validation_alias="WEB_SEARCH_MAX_USES")
    # Structuring an upload emits one large JSON object (blocks + facts); 8192 output
    # tokens truncates a rich document into invalid JSON, so the ingest call uses a
    # higher ceiling. A max_tokens stop-reason still guards against silent truncation.
    ingest_max_tokens: int = Field(default=64000, gt=0, validation_alias="INGEST_MAX_TOKENS")


class CortexConfig(BaseSettings):
    """Config for the Cortex Analyst REST tool, loaded from env / .env.

    Auth to the Analyst REST API is a Programmatic Access Token (PAT) — separate
    from the SQL connection's auth. The generated SQL is still executed over the
    normal SnowflakeConnection, so this only needs the REST endpoint bits.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
    )

    account: str = Field(min_length=1, validation_alias="SNOWFLAKE_ACCOUNT")
    pat: str = Field(min_length=1, validation_alias="SNOWFLAKE_PAT")
    semantic_view: str = Field(min_length=1, validation_alias="CORTEX_SEMANTIC_VIEW")
    timeout_s: float = Field(default=60.0, gt=0, validation_alias="CORTEX_TIMEOUT_S")

    @field_validator("pat", "semantic_view", mode="before")
    @classmethod
    def _blank_to_none(cls, v: object) -> object:
        # Blank -> None so a half-filled .env raises ValidationError (skips cleanly)
        # rather than sending an empty token / view name to the API.
        if isinstance(v, str) and v.strip() == "":
            return None
        return v

    @property
    def base_url(self) -> str:
        return f"https://{self.account}.snowflakecomputing.com"

    @property
    def message_url(self) -> str:
        return f"{self.base_url}/api/v2/cortex/analyst/message"
