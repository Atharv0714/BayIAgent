# shellcheck shell=bash
# shellcheck disable=SC2034  # values are read by deploy.sh, not used in this file
# ==============================================================================
# BayI Agent — deployment configuration
#
# NON-SECRET ONLY. This file is committed. Credentials live in Key Vault and are
# loaded by load-secrets.sh; nothing here is sensitive.
#
# deploy.sh sources this automatically. Override any value inline for a one-off:
#   PLAN_SKU=P0v3 ./deploy.sh
# ==============================================================================

# ── Azure ────────────────────────────────────────────────────────────────────
SUBSCRIPTION="__FILL_IN__"          # set by deploy.sh's caller or edited here
LOCATION="westus2"
RESOURCE_GROUP="rg-bayi-agent"
PLAN_NAME="asp-bayi-agent"
PLAN_SKU="B1"
APP_NAME="bayi-agent"               # -> https://bayi-agent.azurewebsites.net
VAULT_NAME="kv-bayi-agent"
PYTHON_VERSION="3.11"

# ── Snowflake (structure only — account/user/key are in Key Vault) ───────────
# These are the least-privilege roles created by deploy/snowflake-roles.sql.
# They replace the ACCOUNTADMIN posture the app used locally: the query path
# executes model-generated SQL, so it must not be able to write.
SNOWFLAKE_WAREHOUSE="COMPUTE_WH"
SNOWFLAKE_DATABASE="BAYONE_INTERNALINFO"
SNOWFLAKE_SCHEMA="PUBLIC"
SNOWFLAKE_ROLE="BAYI_READ"
SNOWFLAKE_INGEST_ROLE="BAYI_INGEST_WRITE"
SF_ROW_CAP="200"

# ── Cortex ───────────────────────────────────────────────────────────────────
CORTEX_SEMANTIC_VIEW="BAYONE_INTERNALINFO.PUBLIC.BILLABLE_SEMANTIC"
CORTEX_TIMEOUT_S="60"

# ── Agent loop ───────────────────────────────────────────────────────────────
AGENT_MODEL="claude-sonnet-4-6"
AGENT_MAX_ROUNDS="8"
AGENT_MAX_TOKENS="8192"
INGEST_MAX_TOKENS="128000"

# ── Auth / enforcement ───────────────────────────────────────────────────────
# Object ID (GUID) of SG-BayI-Sensitive. Entra emits group OBJECT IDs in the
# token, never display names, and the app compares the claim value literally.
PROTECTED_GROUP_OBJECT_ID="__FILL_IN__"

# Stays false until BOTH are true:
#   1. Entra sign-in is verified end to end, AND
#   2. docs/sql/02_columns.sql + 03_row_access_policy.sql have been applied.
# Without (2) the app sets BAYI_CALLER and no policy reads it — that is not
# isolation, it only looks like it.
ENFORCE_OWNERSHIP="false"
