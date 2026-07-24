#!/usr/bin/env bash
# ==============================================================================
# BayI Intelligence Agent — load secrets into Key Vault
#
# Run this ONCE before the first deploy.sh, and again whenever a credential
# rotates. It is separate from deploy.sh on purpose: deploy.sh is committed and
# runs from a clean clone, while this script needs the two files that are
# deliberately NEVER in the repo (.env and rsa_key.p8).
#
# Usage (from a directory holding your real .env and rsa_key.p8):
#   VAULT_NAME=kv-bayi-agent ./load-secrets.sh /path/to/.env /path/to/rsa_key.p8
#
# Secret values are passed to az via files with umask 077, never as command-line
# arguments, so they do not appear in the process table or your shell history.
# ==============================================================================
set -euo pipefail

VAULT_NAME="${VAULT_NAME:?set VAULT_NAME}"
ENV_FILE="${1:-.env}"
KEY_FILE="${2:-rsa_key.p8}"

step() { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
info() { printf '    %s\n' "$*"; }
die()  { printf '\n\033[1;31mFAILED: %s\033[0m\n' "$*" >&2; exit 1; }

[[ -f "$ENV_FILE" ]] || die "no .env at $ENV_FILE"
[[ -f "$KEY_FILE" ]] || die "no private key at $KEY_FILE"
grep -q "BEGIN.*PRIVATE KEY" "$KEY_FILE" || die "$KEY_FILE is not a PEM private key"

# Cross-check the key's encryption against the passphrase before uploading either.
# Getting this pair wrong surfaces on Azure as an opaque JWT_TOKEN_INVALID at
# connect time, with nothing in the logs pointing at the passphrase.
KEY_IS_ENCRYPTED=false
grep -q "BEGIN ENCRYPTED PRIVATE KEY" "$KEY_FILE" && KEY_IS_ENCRYPTED=true

umask 077
STAGE="$(mktemp -d)"
# Wipe the decoded material no matter how we exit.
trap 'rm -rf "$STAGE"' EXIT

step "Granting yourself write access to $VAULT_NAME"
# deploy.sh grants the APP read access; this grants the OPERATOR write access.
# Separate roles: the app can never create or overwrite a secret.
ME="$(az ad signed-in-user show --query id -o tsv)"
VAULT_ID="$(az keyvault show --name "$VAULT_NAME" --query id -o tsv)"
az role assignment create --assignee-object-id "$ME" --assignee-principal-type User \
    --role "Key Vault Secrets Officer" --scope "$VAULT_ID" -o none 2>/dev/null \
    || info "already assigned"
sleep 20  # RBAC propagation

# Read one variable out of the .env without sourcing it (sourcing would execute
# whatever is in there, and would leak every value into this shell's environment).
read_env() {
    local key="$1" line
    line="$(grep -E "^[[:space:]]*${key}=" "$ENV_FILE" | tail -1 || true)"
    [[ -z "$line" ]] && return 0
    line="${line#*=}"
    line="${line%$'\r'}"                       # CRLF-saved .env
    line="${line%\"}"; line="${line#\"}"
    line="${line%\'}"; line="${line#\'}"
    # A whitespace-only value means "unset" — pydantic's _blank_to_none treats it
    # that way, so storing it would put a secret in the vault that the app ignores.
    # This is not hypothetical: SNOWFLAKE_PRIVATE_KEY_PASSPHRASE in the current
    # .env is a single space against an UNENCRYPTED key.
    [[ -z "${line//[[:space:]]/}" ]] && return 0
    printf '%s' "$line"
}

set_secret() {
    local name="$1" value="$2" required="${3:-required}"
    if [[ -z "$value" ]]; then
        [[ "$required" == "required" ]] && die "$name is empty in $ENV_FILE"
        info "skip: $name (not set — optional)"
        return 0
    fi
    printf '%s' "$value" > "$STAGE/$name"
    az keyvault secret set --vault-name "$VAULT_NAME" --name "$name" \
        --file "$STAGE/$name" -o none
    rm -f "$STAGE/$name"
    info "set: $name (${#value} chars)"
}

step "Reading credentials from $ENV_FILE"
set_secret anthropic-api-key "$(read_env ANTHROPIC_API_KEY)"
set_secret snowflake-account "$(read_env SNOWFLAKE_ACCOUNT)"
set_secret snowflake-user    "$(read_env SNOWFLAKE_USER)"
set_secret snowflake-pat     "$(read_env SNOWFLAKE_PAT)"
PASSPHRASE="$(read_env SNOWFLAKE_PRIVATE_KEY_PASSPHRASE)"
if [[ "$KEY_IS_ENCRYPTED" == true && -z "$PASSPHRASE" ]]; then
    die "$KEY_FILE is an ENCRYPTED private key but no SNOWFLAKE_PRIVATE_KEY_PASSPHRASE is set"
fi
if [[ "$KEY_IS_ENCRYPTED" == false && -n "$PASSPHRASE" ]]; then
    die "$KEY_FILE is UNENCRYPTED but a passphrase is set — the connector will reject it"
fi
set_secret snowflake-private-key-passphrase "$PASSPHRASE" optional

step "Encoding $KEY_FILE"
# Stored base64-encoded because a multi-line PEM does not survive the app-setting
# round trip reliably; startup.sh decodes it back to a file at container start.
base64 -w0 < "$KEY_FILE" > "$STAGE/pem.b64" 2>/dev/null || base64 < "$KEY_FILE" | tr -d '\n' > "$STAGE/pem.b64"
az keyvault secret set --vault-name "$VAULT_NAME" --name snowflake-private-key-pem-b64 \
    --file "$STAGE/pem.b64" -o none
info "set: snowflake-private-key-pem-b64 ($(wc -c < "$STAGE/pem.b64") chars)"

step "Verifying the round trip"
# Decode the stored secret back and confirm it is byte-identical to the source key.
# A silently truncated or re-wrapped key fails much later as an opaque JWT error.
az keyvault secret show --vault-name "$VAULT_NAME" --name snowflake-private-key-pem-b64 \
    --query value -o tsv | base64 -d > "$STAGE/roundtrip.p8"
if cmp -s "$KEY_FILE" "$STAGE/roundtrip.p8"; then
    info "private key round-trips byte-for-byte"
else
    die "round-trip mismatch — the stored key does not match $KEY_FILE"
fi

step "Secrets in $VAULT_NAME"
az keyvault secret list --vault-name "$VAULT_NAME" \
    --query "[].{name:name, updated:attributes.updated}" -o table

cat <<'EOF'

Done. Next: run ./deploy.sh

If you uploaded .env / rsa_key.p8 into Cloud Shell to run this, delete them now:
    shred -u ~/.env ~/rsa_key.p8 2>/dev/null || rm -f ~/.env ~/rsa_key.p8
EOF
