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
# These reuse resources that ALREADY EXISTED in the subscription. BayIInternalAgent
# was an empty shell — no app settings, no managed identity, no auth, no startup
# command — on a P1v3 plan that was already billing. Configuring it in place beats
# standing up a second app next to it.
SUBSCRIPTION="068a6714-1eee-423b-b018-add1c766c033"
LOCATION="centralus"                # follows the existing plan, not a free choice
RESOURCE_GROUP="BayIAgent"
PLAN_NAME="ASP-BayIAgent-ba56"
PLAN_SKU="P1v3"                     # existing SKU; only used if the plan is absent
APP_NAME="BayIInternalAgent"
VAULT_NAME="kv-bayi-agent"          # created by deploy.sh; no vault existed
PYTHON_VERSION="3.11"               # app was created as 3.12; deploy.sh moves it

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
# Endpoint base URL. Blank = Anthropic (default). For Z.AI GLM set
# ANTHROPIC_BASE_URL="https://api.z.ai/api/anthropic", point the anthropic-api-key Key
# Vault secret at the Z.AI key, and set AGENT_MODEL="glm-5.2".
ANTHROPIC_BASE_URL=""
AGENT_MODEL="claude-sonnet-4-6"
AGENT_MAX_ROUNDS="8"
AGENT_MAX_TOKENS="8192"
INGEST_MAX_TOKENS="128000"

# ── Auth / enforcement ───────────────────────────────────────────────────────
# Object ID (GUID) of the group gating the 'protected' tier. Entra emits group
# OBJECT IDs in the token, never display names, and the app compares the claim
# value literally — so the string "SG-BayI-Sensitive" would never match anything.
#
# PLACEHOLDER: no such group exists in the bayone.com tenant yet. All-zeros never
# matches any claim, so every membership check is false and protected rows stay
# unreadable. That is the fail-closed direction and it is deliberate.
#
# To switch it on later: create the group, then
#   az ad group show --group SG-BayI-Sensitive --query id -o tsv
# put the GUID here, re-run deploy.sh, and restart. The tier stays inert until
# docs/sql/02_columns.sql and 03_row_access_policy.sql have also been applied.
PROTECTED_GROUP_OBJECT_ID="00000000-0000-0000-0000-000000000000"

# Stays false until BOTH are true:
#   1. Entra sign-in is verified end to end, AND
#   2. docs/sql/02_columns.sql + 03_row_access_policy.sql have been applied.
# Without (2) the app sets BAYI_CALLER and no policy reads it — that is not
# isolation, it only looks like it.
ENFORCE_OWNERSHIP="false"
