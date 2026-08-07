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

# ── Agent loop / model provider ──────────────────────────────────────────────
# The query lane runs GLM on Z.AI through Anthropic's compatible endpoint — much
# cheaper per question than Claude. ANTHROPIC_API_KEY therefore holds the Z.AI
# key, NOT an Anthropic key.
ANTHROPIC_BASE_URL="https://api.z.ai/api/anthropic"
AGENT_MODEL="glm-5.2"
AGENT_MAX_ROUNDS="8"
AGENT_MAX_TOKENS="8192"
INGEST_MAX_TOKENS="128000"

# ── Document-vision lane ─────────────────────────────────────────────────────
# GLM cannot read PDFs/Office files, so ingest of those routes to a provider with
# real document vision (Anthropic) while ordinary queries stay on the cheap one.
# INGEST_VISION_API_KEY is the *Anthropic* key and lives in Key Vault separately.
# Leave INGEST_VISION_BASE_URL unset to use Anthropic's default endpoint.
INGEST_VISION_MODEL="claude-sonnet-4-6"

# ── Cortex Analyst ───────────────────────────────────────────────────────────
# Disabled: the semantic view route was turned off in favour of the SQL agent.
# SNOWFLAKE_PAT stays in Key Vault so re-enabling is a one-line change.
CORTEX_ENABLED="false"

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

# Named members of the 'protected' tier, comma-separated, OR'd with the group above.
# This is the mechanism actually in use: the Entra group needs a Groups Administrator to
# create it AND the app registration configured to emit the groups claim, while this needs
# only the UPN that sign-in already delivers. Moving to the group later is just filling in
# PROTECTED_GROUP_OBJECT_ID — no code change, and both keep working.
#
# LEFT EMPTY ON PURPOSE — this repo is public. Naming the account with elevated access to
# the protected tier tells a reader exactly which identity to target, so the value is passed
# at deploy time and lives only in the Azure app settings:
#
#   PROTECTED_USERS=someone@bayone.com bash deploy/deploy.sh
#
# deploy.sh reads it from the environment, so an inline value ships normally. Re-run with it
# set whenever the membership changes; omitting it on a later run would clear the tier.
#
# Use the EXACT string /api/whoami reports as "identity" while signed in. It is the UPN
# Easy Auth puts in X-MS-CLIENT-PRINCIPAL-NAME, which is not always the mail address you
# would guess. (The comparison is case-insensitive, so casing alone will not break it.)
# Empty = nobody is in the protected tier, which is the fail-closed direction.
PROTECTED_USERS=""

# Requires BOTH of these, and both are now true:
#   1. Entra sign-in verified end to end — the deployed site returns 401 unauthenticated
#      on /, /api/whoami and /.auth/me, and /api/whoami reports a real UPN once signed in.
#   2. The governance SQL applied — rap_ownership is ACTIVE on BLOCKS and FACTS, and the
#      app connects as BAYI_READ (see SNOWFLAKE_ROLE above), which the policy does not
#      exempt. Without (2) the app would set BAYI_CALLER and no policy would read it —
#      that is not isolation, it only looks like it.
#
# Set here rather than by `az webapp config appsettings set`, because deploy.sh writes this
# value on every run: flipping it by CLI alone is silently reverted by the next deploy.
ENFORCE_OWNERSHIP="true"
