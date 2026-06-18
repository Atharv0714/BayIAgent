from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class SnowflakeConfig(BaseSettings):
    """Typed Snowflake connection config, loaded from env / .env.

    Auth is key-pair only: provide a PEM private key path (+ passphrase if the key
    is encrypted). No password field exists by design.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    account: str = Field(validation_alias="SNOWFLAKE_ACCOUNT")
    user: str = Field(validation_alias="SNOWFLAKE_USER")
    private_key_path: str = Field(validation_alias="SNOWFLAKE_PRIVATE_KEY_PATH")
    private_key_passphrase: str | None = Field(
        default=None, validation_alias="SNOWFLAKE_PRIVATE_KEY_PASSPHRASE"
    )
    warehouse: str = Field(validation_alias="SNOWFLAKE_WAREHOUSE")
    database: str = Field(validation_alias="SNOWFLAKE_DATABASE")
    schema_name: str = Field(validation_alias="SNOWFLAKE_SCHEMA")
    role: str = Field(validation_alias="SNOWFLAKE_ROLE")

    row_cap: int = Field(default=1000, gt=0, validation_alias="SF_ROW_CAP")
