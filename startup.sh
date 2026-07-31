#!/usr/bin/env bash
# Azure App Service (Linux) startup command for the BayI Intelligence Agent.
#
# Wired up as:   az webapp config set --startup-file "bash startup.sh"
#
# NOTE THE RELATIVE PATH. Oryx builds this app into a compressed
# /home/site/wwwroot/output.tar.zst and the platform extracts it to a temp
# directory at container start — so at runtime the app is NOT in wwwroot, and an
# absolute /home/site/wwwroot/startup.sh does not exist. Referencing the script
# relatively (and deriving APP_DIR from its own location) is what makes this work;
# the earlier absolute-path version failed with exit code 127 on every boot.
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# /home is the persisted Azure Files mount, so the key survives a restart. It is
# also OUTSIDE the extracted app dir, which is discarded on each deploy.
KEY_PATH="/home/site/rsa_key.p8"

log() { echo "[startup] $*"; }
log "app dir: $APP_DIR"

# ── 1. Python path ───────────────────────────────────────────────────────────
# requirements.txt is exported with --no-emit-project, so the sf_agent package is
# not pip-installed; it has to be importable by path.
export PYTHONPATH="$APP_DIR/src${PYTHONPATH:+:$PYTHONPATH}"

# ── 2. Materialize the Snowflake private key ─────────────────────────────────
# SNOWFLAKE_PRIVATE_KEY_PEM_B64 is a Key Vault reference holding base64 of the
# .p8. Base64 is deliberate: a multi-line PEM does not round-trip through an app
# setting reliably, and a mangled key fails with an opaque JWT error at connect.
if [ -n "${SNOWFLAKE_PRIVATE_KEY_PEM_B64:-}" ]; then
    umask 077
    printf '%s' "$SNOWFLAKE_PRIVATE_KEY_PEM_B64" | base64 -d > "$KEY_PATH"
    chmod 600 "$KEY_PATH"
    if ! grep -q "BEGIN.*PRIVATE KEY" "$KEY_PATH"; then
        log "FATAL: decoded key at $KEY_PATH is not a PEM private key."
        log "       Check the Key Vault secret holds base64 of the .p8 file."
        exit 1
    fi
    export SNOWFLAKE_PRIVATE_KEY_PATH="$KEY_PATH"
    log "private key materialized ($(wc -c < "$KEY_PATH") bytes)"
else
    log "FATAL: SNOWFLAKE_PRIVATE_KEY_PEM_B64 is empty."
    log "       Key Vault reference unresolved — check the web app's managed identity"
    log "       has get/list on the vault's secrets, then restart."
    exit 1
fi

# The config validator rejects having BOTH auth methods set.
unset SNOWFLAKE_PASSWORD || true

# ── 3. Serve ─────────────────────────────────────────────────────────────────
# --workers 1 is load-bearing: sf_agent holds a SINGLE shared Snowflake connection
# behind a module-level lock, so each extra worker opens its own warehouse session
# and multiplies credit burn without adding throughput.
# --timeout 120 covers the long agent loop (multi-round SQL + model calls).
log "starting gunicorn on port ${PORT:-8000}"
exec gunicorn sf_agent.web:app \
    --worker-class uvicorn.workers.UvicornWorker \
    --bind "0.0.0.0:${PORT:-8000}" \
    --workers 1 \
    --timeout 120 \
    --access-logfile '-' \
    --error-logfile '-'
