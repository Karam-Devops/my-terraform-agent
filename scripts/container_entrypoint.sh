#!/bin/sh
# scripts/container_entrypoint.sh
#
# Cloud Run container entrypoint: auto-configure gcloud CLI from the
# metadata server, then exec Streamlit.
#
# Why this exists: Cloud Run auto-provides Application Default
# Credentials (ADC) via the metadata server, which Python SDKs
# (google-cloud-*, google-auth) pick up automatically. The gcloud
# CLI does NOT -- it requires `account` and `project` to be explicitly
# set in its local config. Without that, every `gcloud storage rsync`
# / `gcloud asset` call fails with:
#
#   ERROR: ... You do not currently have an active account selected.
#   Please run: $ gcloud auth login
#
# Surfaced during PUI-1 SMOKE (2026-04-28) on the first hydrate that
# successfully reached the gcloud subprocess (after layers 1-3 were
# unblocked).
#
# What this script does:
#   1. Queries the Cloud Run metadata server for the runtime SA email
#   2. Runs `gcloud config set account / project` so subsequent
#      gcloud subprocess calls inherit the SA identity
#   3. Exec's Streamlit (replacing this shell process so signal
#      handling stays correct under tini)
#
# Local-dev path: if the metadata server is unreachable (no Cloud Run
# context), skip the gcloud config and trust the operator's `gcloud
# auth login`. Same script works in both environments.

set -e

METADATA_URL="http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/email"

if SA_EMAIL=$(curl -sf -m 2 -H 'Metadata-Flavor: Google' "$METADATA_URL" 2>/dev/null); then
    echo "[entrypoint] runtime SA from metadata server: $SA_EMAIL"
    gcloud config set account "$SA_EMAIL" --quiet 2>/dev/null || \
        echo "[entrypoint] WARN: gcloud config set account failed (non-fatal)"
    gcloud config set project "${HOST_PROJECT_ID:-mtagent-internal-dev}" --quiet 2>/dev/null || \
        echo "[entrypoint] WARN: gcloud config set project failed (non-fatal)"
    # Disable usage reporting / interactive prompts in subprocess gcloud
    # invocations -- the engines parse stdout, prompts would corrupt it.
    gcloud config set core/disable_prompts true --quiet 2>/dev/null || true
else
    echo "[entrypoint] metadata server unreachable; assuming local-dev (no gcloud auto-config)"
fi

# Exec replaces this shell PID with streamlit's, so tini's signal
# forwarding lands on the actual process, not this wrapper.
exec streamlit run app/main.py \
    --server.port="${PORT:-8080}" \
    --server.address=0.0.0.0 \
    --server.headless=true
