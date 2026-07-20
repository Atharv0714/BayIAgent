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


class AuthConfig(BaseSettings):
    """Config for per-owner private-data enforcement, loaded from env / .env.

    All defaults preserve today's behavior: with ``enforce_ownership`` false the app
    binds no caller identity and every row stays shared (``internal``), so it runs
    locally exactly as before. Flip ``ENFORCE_OWNERSHIP=true`` only after the Snowflake
    migration in ``docs/sql/`` has been applied.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
    )

    # Master switch. When false, the query path binds nothing and ingest stamps
    # everything 'internal' — identical to the pre-feature behavior.
    enforce_ownership: bool = Field(default=False, validation_alias="ENFORCE_OWNERSHIP")
    # Snowflake session variable the app sets to the caller's identity each request.
    # MUST match the variable name referenced in the rap_ownership policy body.
    caller_session_var: str = Field(default="BAYI_CALLER", validation_alias="CALLER_SESSION_VAR")
    # Request header Azure App Service "Easy Auth" injects with the signed-in user's
    # identity (server-trusted; never set by the browser).
    easy_auth_header: str = Field(
        default="X-MS-CLIENT-PRINCIPAL-NAME", validation_alias="EASY_AUTH_HEADER"
    )
    # Local fallback identity used when the Easy Auth header is absent (dev only).
    dev_caller_identity: str | None = Field(
        default=None, validation_alias="DEV_CALLER_IDENTITY"
    )

    # ── protected (group-scoped) tier ────────────────────────────────────────
    # The single privileged Entra/M365 group whose members may mark uploads
    # 'protected' and read protected rows (the old SG-BayI-Sensitive). Membership
    # is decided per request from the caller's group claim, never from row data.
    protected_group: str = Field(
        default="SG-BayI-Sensitive", validation_alias="PROTECTED_GROUP"
    )
    # Request header carrying the caller's group memberships (server-trusted, from
    # SharePoint/M365 Entra). Comma-separated group names/ids. On Azure Easy Auth
    # the full claims arrive base64 in X-MS-CLIENT-PRINCIPAL; the deployment maps
    # the groups claim onto this simple header. Configurable to match whatever
    # SharePoint/M365 surfaces.
    groups_header: str = Field(
        default="X-MS-CLIENT-GROUPS", validation_alias="GROUPS_HEADER"
    )
    # Snowflake session variable the app sets to 'true'/'false' each request to tell
    # the row-access policy whether the caller is in protected_group. MUST match the
    # variable name referenced in the rap_ownership policy body.
    protected_session_var: str = Field(
        default="BAYI_PROTECTED", validation_alias="PROTECTED_SESSION_VAR"
    )
    # Local fallback group list (comma-separated) used when groups_header is absent
    # (dev only). Set to include protected_group to test protected ingest/queries.
    dev_caller_groups: str | None = Field(
        default=None, validation_alias="DEV_CALLER_GROUPS"
    )

    @field_validator("dev_caller_identity", "dev_caller_groups", mode="before")
    @classmethod
    def _blank_to_none(cls, v: object) -> object:
        if isinstance(v, str) and v.strip() == "":
            return None
        return v

    @property
    def dev_group_list(self) -> list[str]:
        """dev_caller_groups parsed into a stripped, non-empty list."""
        if not self.dev_caller_groups:
            return []
        return [g.strip() for g in self.dev_caller_groups.split(",") if g.strip()]


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
