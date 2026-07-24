#!/usr/bin/env bash
# ==============================================================================
# BayI Intelligence Agent — Azure App Service deployment
#
# Idempotent. Safe to re-run: every step either creates a resource or updates it
# in place. Re-run after changing app settings, after a code change, or to repair
# drift. It does NOT put any secret on disk or in an app setting in plaintext —
# secret VALUES are loaded separately by ./load-secrets.sh and referenced here
# only by Key Vault URI.
#
# Usage:
#   ./deploy.sh              # full deploy
#   ./deploy.sh --code-only  # skip infra, just push code and restart
#
# Prerequisites:
#   * az CLI, logged in (Azure Cloud Shell already is)
#   * ./load-secrets.sh has been run at least once
# ==============================================================================
set -euo pipefail

# ── Configuration ────────────────────────────────────────────────────────────
# Defaults come from config.sh (committed, non-secret). Anything already set in
# the environment WINS, so a one-off override needs no file edit:
#   PLAN_SKU=P0v3 ./deploy.sh
CONFIG_FILE="${CONFIG_FILE:-$(dirname "${BASH_SOURCE[0]}")/config.sh}"
if [[ -f "$CONFIG_FILE" ]]; then
    while IFS= read -r line; do
        [[ "$line" =~ ^[[:space:]]*# || -z "${line// /}" ]] && continue
        # KEY="value"  with an optional trailing # comment. Matching the closing
        # quote explicitly is what keeps the comment out of the value — a plain
        # suffix-strip silently appends it, and the failure surfaces much later as
        # an unparseable resource name.
        [[ "$line" =~ ^[[:space:]]*([A-Za-z_][A-Za-z0-9_]*)=\"([^\"]*)\" ]] || continue
        key="${BASH_REMATCH[1]}"
        [[ -n "${!key-}" ]] && continue          # environment overrides the file
        printf -v "$key" '%s' "${BASH_REMATCH[2]}"
        export "${key?}"
    done < "$CONFIG_FILE"
fi

SUBSCRIPTION="${SUBSCRIPTION:?set SUBSCRIPTION to the target subscription id or name}"
LOCATION="${LOCATION:-westus2}"
RESOURCE_GROUP="${RESOURCE_GROUP:-rg-bayi-agent}"
PLAN_NAME="${PLAN_NAME:-asp-bayi-agent}"
PLAN_SKU="${PLAN_SKU:-B1}"
APP_NAME="${APP_NAME:?set APP_NAME — must be globally unique, becomes <name>.azurewebsites.net}"
VAULT_NAME="${VAULT_NAME:?set VAULT_NAME — must be globally unique, max 24 chars}"
PYTHON_VERSION="${PYTHON_VERSION:-3.11}"

# Entra group whose members may read the 'protected' data tier. This is the group's
# OBJECT ID, not its display name: Entra emits group GUIDs in the token, and the app
# compares the claim value literally. See runbook "Rotating the protected group".
PROTECTED_GROUP_OBJECT_ID="${PROTECTED_GROUP_OBJECT_ID:?set PROTECTED_GROUP_OBJECT_ID to the SG-BayI-Sensitive object id}"

# Ownership enforcement stays OFF until Entra sign-in is verified end to end.
# Flipping this before auth works would bind a null caller identity into the
# Snowflake row-access policy. The runbook covers turning it on.
ENFORCE_OWNERSHIP="${ENFORCE_OWNERSHIP:-false}"

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CODE_ONLY=false
[[ "${1:-}" == "--code-only" ]] && CODE_ONLY=true

step() { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
info() { printf '    %s\n' "$*"; }
die()  { printf '\n\033[1;31mFAILED: %s\033[0m\n' "$*" >&2; exit 1; }

az account set --subscription "$SUBSCRIPTION"
info "subscription: $(az account show --query name -o tsv)"

# ==============================================================================
# INFRASTRUCTURE
# ==============================================================================
if [[ "$CODE_ONLY" == false ]]; then

step "Resource group: $RESOURCE_GROUP ($LOCATION)"
az group create --name "$RESOURCE_GROUP" --location "$LOCATION" -o none

step "Verifying Python $PYTHON_VERSION is still offered on Linux App Service"
# App Service retires runtimes independently of upstream Python. Fail loudly here
# rather than with an opaque error after the plan is already billing.
if ! az webapp list-runtimes --os-type linux -o tsv | grep -qi "^PYTHON:${PYTHON_VERSION}$"; then
    info "available Python runtimes:"
    az webapp list-runtimes --os-type linux -o tsv | grep -i '^PYTHON' | sed 's/^/      /'
    die "PYTHON:${PYTHON_VERSION} is not offered. Pick a supported version and set PYTHON_VERSION."
fi

step "App Service plan: $PLAN_NAME"
# Never resize a plan that already exists — the SKU is a billing decision, and
# silently moving someone's P1v3 to B1 (or the reverse) is not this script's call.
if EXISTING_SKU="$(az appservice plan show --name "$PLAN_NAME" --resource-group "$RESOURCE_GROUP" \
        --query sku.name -o tsv 2>/dev/null)" && [[ -n "$EXISTING_SKU" ]]; then
    info "exists at SKU $EXISTING_SKU — reusing, not resizing"
    [[ "$EXISTING_SKU" != "$PLAN_SKU" ]] && \
        info "note: config.sh says $PLAN_SKU. To change it: az appservice plan update --sku $PLAN_SKU"
else
    info "creating at $PLAN_SKU"
    az appservice plan create \
        --name "$PLAN_NAME" --resource-group "$RESOURCE_GROUP" \
        --location "$LOCATION" --sku "$PLAN_SKU" --is-linux -o none
fi

step "Web app: $APP_NAME"
az webapp create \
    --name "$APP_NAME" --resource-group "$RESOURCE_GROUP" --plan "$PLAN_NAME" \
    --runtime "PYTHON:${PYTHON_VERSION}" -o none 2>/dev/null \
    || info "already exists — leaving in place"

# --runtime above is only honoured on CREATE. An app that already exists keeps
# whatever stack it was made with (BayIInternalAgent was created as 3.12), so set
# it explicitly. This is what pins the app to the interpreter uv.lock resolved for.
CURRENT_FX="$(az webapp config show --name "$APP_NAME" --resource-group "$RESOURCE_GROUP" \
    --query linuxFxVersion -o tsv)"
if [[ "$CURRENT_FX" != "PYTHON|${PYTHON_VERSION}" ]]; then
    info "runtime is $CURRENT_FX — setting PYTHON|${PYTHON_VERSION}"
    az webapp config set --name "$APP_NAME" --resource-group "$RESOURCE_GROUP" \
        --linux-fx-version "PYTHON|${PYTHON_VERSION}" -o none
else
    info "runtime already PYTHON|${PYTHON_VERSION}"
fi

step "Key Vault: $VAULT_NAME (RBAC authorization)"
az keyvault create \
    --name "$VAULT_NAME" --resource-group "$RESOURCE_GROUP" --location "$LOCATION" \
    --enable-rbac-authorization true \
    --retention-days 7 -o none 2>/dev/null \
    || info "already exists — leaving in place"
VAULT_URI="https://${VAULT_NAME}.vault.azure.net"

step "Managed identity for $APP_NAME"
PRINCIPAL_ID="$(az webapp identity assign \
    --name "$APP_NAME" --resource-group "$RESOURCE_GROUP" \
    --query principalId -o tsv)"
info "principal: $PRINCIPAL_ID"

step "Granting the app read access to vault secrets"
VAULT_ID="$(az keyvault show --name "$VAULT_NAME" --query id -o tsv)"
# 'Key Vault Secrets User' = get/list secret VALUES, nothing else. The app never
# needs to write, and cannot enumerate keys or certificates.
az role assignment create \
    --assignee-object-id "$PRINCIPAL_ID" --assignee-principal-type ServicePrincipal \
    --role "Key Vault Secrets User" --scope "$VAULT_ID" -o none 2>/dev/null \
    || info "role assignment already present"

# RBAC is eventually consistent. A Key Vault reference evaluated before the grant
# lands sticks in a failed state until the next restart, which looks exactly like
# a misconfigured secret — so wait it out here instead of debugging it later.
info "waiting 60s for RBAC propagation before wiring Key Vault references"
sleep 60

fi  # end infra

# ==============================================================================
# APP SETTINGS
# ==============================================================================
VAULT_URI="https://${VAULT_NAME}.vault.azure.net"
kv() { echo "@Microsoft.KeyVault(SecretUri=${VAULT_URI}/secrets/$1/)"; }

step "Checking required secrets exist in $VAULT_NAME"
REQUIRED_SECRETS=(
    anthropic-api-key
    snowflake-account
    snowflake-user
    snowflake-pat
    snowflake-private-key-pem-b64
)
for s in "${REQUIRED_SECRETS[@]}"; do
    az keyvault secret show --vault-name "$VAULT_NAME" --name "$s" -o none 2>/dev/null \
        || die "secret '$s' missing from $VAULT_NAME. Run ./load-secrets.sh first."
    info "ok: $s"
done
# Optional: only present when the private key is encrypted.
PASSPHRASE_SETTING=""
if az keyvault secret show --vault-name "$VAULT_NAME" --name snowflake-private-key-passphrase -o none 2>/dev/null; then
    PASSPHRASE_SETTING="SNOWFLAKE_PRIVATE_KEY_PASSPHRASE=$(kv snowflake-private-key-passphrase)"
    info "ok: snowflake-private-key-passphrase (key is encrypted)"
else
    info "no passphrase secret — treating the private key as unencrypted"
fi

step "Applying app settings"
# NOTE ON WHAT IS AND IS NOT A SECRET:
#   Key Vault references  -> credentials and account identifiers.
#   Plain settings        -> non-sensitive structure (warehouse/db/schema/role names,
#                            model tuning, header names). These are visible to anyone
#                            with Reader on the app, which is intended.
# DELIBERATELY ABSENT:
#   SNOWFLAKE_PASSWORD          — auth is key-pair; config rejects both being set.
#   SNOWFLAKE_PRIVATE_KEY_PATH  — exported by startup.sh after it writes the PEM.
#   DEV_CALLER_IDENTITY/GROUPS  — local-only fallbacks; setting them on Azure would
#                                 hand every unauthenticated request an identity.
# shellcheck disable=SC2046
az webapp config appsettings set \
    --name "$APP_NAME" --resource-group "$RESOURCE_GROUP" -o none \
    --settings \
    "ANTHROPIC_API_KEY=$(kv anthropic-api-key)" \
    "SNOWFLAKE_ACCOUNT=$(kv snowflake-account)" \
    "SNOWFLAKE_USER=$(kv snowflake-user)" \
    "SNOWFLAKE_PAT=$(kv snowflake-pat)" \
    "SNOWFLAKE_PRIVATE_KEY_PEM_B64=$(kv snowflake-private-key-pem-b64)" \
    ${PASSPHRASE_SETTING:+"$PASSPHRASE_SETTING"} \
    "SNOWFLAKE_WAREHOUSE=${SNOWFLAKE_WAREHOUSE:?}" \
    "SNOWFLAKE_DATABASE=${SNOWFLAKE_DATABASE:?}" \
    "SNOWFLAKE_SCHEMA=${SNOWFLAKE_SCHEMA:?}" \
    "SNOWFLAKE_ROLE=${SNOWFLAKE_ROLE:?}" \
    "SNOWFLAKE_INGEST_ROLE=${SNOWFLAKE_INGEST_ROLE:?}" \
    "CORTEX_SEMANTIC_VIEW=${CORTEX_SEMANTIC_VIEW:?}" \
    "SF_ROW_CAP=${SF_ROW_CAP:-200}" \
    "CORTEX_TIMEOUT_S=${CORTEX_TIMEOUT_S:-60}" \
    "AGENT_MODEL=${AGENT_MODEL:-claude-sonnet-4-6}" \
    "AGENT_MAX_ROUNDS=${AGENT_MAX_ROUNDS:-8}" \
    "AGENT_MAX_TOKENS=${AGENT_MAX_TOKENS:-8192}" \
    "INGEST_MAX_TOKENS=${INGEST_MAX_TOKENS:-128000}" \
    "ENFORCE_OWNERSHIP=${ENFORCE_OWNERSHIP}" \
    "CALLER_SESSION_VAR=BAYI_CALLER" \
    "PROTECTED_SESSION_VAR=BAYI_PROTECTED" \
    "EASY_AUTH_HEADER=X-MS-CLIENT-PRINCIPAL-NAME" \
    "GROUPS_HEADER=X-MS-CLIENT-PRINCIPAL" \
    "PROTECTED_GROUP=${PROTECTED_GROUP_OBJECT_ID}" \
    "SCM_DO_BUILD_DURING_DEPLOYMENT=true" \
    "ENABLE_ORYX_BUILD=true" \
    "WEBSITES_ENABLE_APP_SERVICE_STORAGE=true" \
    "PYTHONUNBUFFERED=1"

# GROUPS_HEADER=X-MS-CLIENT-PRINCIPAL is not a typo. Easy Auth has no
# X-MS-CLIENT-GROUPS header; group claims arrive inside the base64 principal blob,
# which sf_agent.web._principal_groups decodes. See the runbook.

step "Platform settings: HTTPS-only, Always On, startup command"
az webapp update --name "$APP_NAME" --resource-group "$RESOURCE_GROUP" \
    --https-only true -o none
az webapp config set --name "$APP_NAME" --resource-group "$RESOURCE_GROUP" \
    --always-on true \
    --startup-file "/home/site/wwwroot/startup.sh" \
    --ftps-state Disabled -o none

step "Enabling application logging"
az webapp log config --name "$APP_NAME" --resource-group "$RESOURCE_GROUP" \
    --application-logging filesystem --detailed-error-messages true \
    --failed-request-tracing true --level information -o none

# ==============================================================================
# CODE
# ==============================================================================
step "Packaging application"
ZIP="$(mktemp -d)/app.zip"
pushd "$REPO_ROOT" >/dev/null
# Ship only what the runtime needs. The excludes are a safety net, not the primary
# control — .env and rsa_key.p8 are gitignored and absent from a fresh clone — but
# this script may also be run from a working tree where they DO exist.
zip -qr "$ZIP" \
    src requirements.txt startup.sh pyproject.toml README.md \
    -x '*.pyc' '*__pycache__*' '*.env*' '*rsa_key*' '*.git*' \
       '*.venv*' '*.pytest_cache*' '*.ingest_drafts*'
popd >/dev/null

if unzip -l "$ZIP" | grep -Eq 'rsa_key|(^|/)\.env'; then
    die "package contains a secret file — aborting before upload"
fi
info "package: $(du -h "$ZIP" | cut -f1), $(unzip -l "$ZIP" | tail -1 | awk '{print $2}') files"

step "Deploying (Oryx will install requirements.txt — this takes a few minutes)"
az webapp deploy --name "$APP_NAME" --resource-group "$RESOURCE_GROUP" \
    --src-path "$ZIP" --type zip --async false -o none
rm -f "$ZIP"

step "Restarting"
az webapp restart --name "$APP_NAME" --resource-group "$RESOURCE_GROUP" -o none

# ==============================================================================
# VERIFY
# ==============================================================================
URL="https://${APP_NAME}.azurewebsites.net"
step "Waiting for the app to answer on $URL"
for i in $(seq 1 40); do
    CODE="$(curl -s -o /dev/null -w '%{http_code}' --max-time 20 "$URL/" || true)"
    # 200 = up and unauthenticated. 302/401 = up and Easy Auth is enforcing, which
    # is the expected steady state once authentication is configured.
    case "$CODE" in
        200) info "HTTP 200 — up (no auth enforced yet)"; break ;;
        302|401) info "HTTP $CODE — up, Easy Auth is enforcing sign-in"; break ;;
        *) printf '    attempt %2d/40: HTTP %s\n' "$i" "$CODE"; sleep 15 ;;
    esac
    [[ "$i" == 40 ]] && die "app never became reachable. Check: az webapp log tail -n $APP_NAME -g $RESOURCE_GROUP"
done

cat <<EOF

────────────────────────────────────────────────────────────────
  URL            $URL
  Logs           az webapp log tail -n $APP_NAME -g $RESOURCE_GROUP
  Settings       az webapp config appsettings list -n $APP_NAME -g $RESOURCE_GROUP -o table
  Enforcement    ENFORCE_OWNERSHIP=$ENFORCE_OWNERSHIP
────────────────────────────────────────────────────────────────
EOF
