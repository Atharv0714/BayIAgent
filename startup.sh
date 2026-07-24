#!/usr/bin/env bash
# Azure App Service (Linux) startup command for the BayI Intelligence Agent.
#
# Runs on every container start, BEFORE gunicorn. Its job is to turn the
# Key Vault-backed app settings into the on-disk state the app expects:
# Snowflake key-pair auth reads a PEM from a FILE PATH, and there is no file
# in the image (the key is deliberately never committed or baked in).
#
# Wired up with:
#   az webapp config set --startup-file "/home/site/wwwroot/startup.sh"
set -euo pipefail

WWWROOT="/home/site/wwwroot"
# /home is the persisted Azure Files mount — anything written here survives a
# container restart. /tmp does not.
KEY_PATH="/home/site/rsa_key.p8"

log() { echo "[startup] $*"; }

# ── 1. Python environment ────────────────────────────────────────────────────
# Oryx installs requirements.txt into a virtualenv at $WWWROOT/antenv. A custom
# startup command bypasses the wrapper that would normally activate it, so do it
# explicitly rather than relying on image-specific behaviour.
if [ -d "$WWWROOT/antenv" ]; then
    log "activating Oryx virtualenv"
    # shellcheck disable=SC1091
    source "$WWWROOT/antenv/bin/activate"
else
    log "WARNING: no antenv found; relying on system site-packages"
fi

# The project is a src-layout package that is NOT pip-installed (requirements.txt
# is exported with --no-emit-project), so sf_agent has to be importable by path.
export PYTHONPATH="$WWWROOT/src${PYTHONPATH:+:$PYTHONPATH}"

# ── 2. Materialize the Snowflake private key ─────────────────────────────────
# SNOWFLAKE_PRIVATE_KEY_PEM_B64 is a Key Vault reference holding the base64 of
# rsa_key.p8. Base64 is deliberate: an app setting round-trips a multi-line PEM
# unreliably, and a mangled key fails with an opaque JWT error at connect time.
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
    log "private key materialized at $KEY_PATH ($(wc -c < "$KEY_PATH") bytes)"
else
    log "FATAL: SNOWFLAKE_PRIVATE_KEY_PEM_B64 is empty."
    log "       Key Vault reference unresolved — check the web app's managed"
    log "       identity has 'Key Vault Secrets User' on the vault, then restart."
    exit 1
fi

# The app's config validator rejects having BOTH auth methods set. An empty-string
# app setting counts as unset there, but unset it here too so the intent is plain.
unset SNOWFLAKE_PASSWORD || true

# ── 3. Serve ─────────────────────────────────────────────────────────────────
# --workers 1 is deliberate and load-bearing: sf_agent holds a SINGLE shared
# Snowflake connection serialized by a module-level lock, so each additional
# worker opens its own warehouse session and multiplies credit burn without
# adding throughput. Raise to 2 only with a matching warehouse review.
# --timeout 120 covers the long agent loop (multi-round SQL + model calls).
log "starting gunicorn on port ${PORT:-8000}"
exec gunicorn sf_agent.web:app \
    --worker-class uvicorn.workers.UvicornWorker \
    --bind "0.0.0.0:${PORT:-8000}" \
    --workers 1 \
    --timeout 120 \
    --access-logfile '-' \
    --error-logfile '-'
