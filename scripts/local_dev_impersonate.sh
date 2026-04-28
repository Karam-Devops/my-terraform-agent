#!/usr/bin/env bash
# scripts/local_dev_impersonate.sh
#
# Operator-side helper: configure your gcloud session to impersonate
# the mtagent runtime SA so local-dev gcloud calls behave EXACTLY
# like the Cloud Run runtime would.
#
# Why this matters: in production, every gcloud call from Cloud Run
# runs as mtagent-runtime@<host-project>.iam.gserviceaccount.com.
# In local dev, your gcloud runs as YOUR personal account by default.
# That's a different identity with different IAM grants -- a customer
# scan that works locally might fail in Cloud Run (your account has
# extra permissions theirs doesn't), or vice versa.
#
# Two pieces this script wires up:
#   1. Grant your operator email iam.serviceAccountTokenCreator on
#      the runtime SA (one-time, idempotent). This lets you mint
#      impersonation tokens for the SA.
#   2. Set gcloud config so EVERY subsequent gcloud call in this
#      shell uses --impersonate-service-account by default.
#
# Usage:
#   OPERATOR_EMAIL=you@example.com \
#   HOST_PROJECT_ID=mtagent-internal-dev \
#     ./scripts/local_dev_impersonate.sh
#
# To revert:
#   gcloud config unset auth/impersonate_service_account
#
# To verify it took effect:
#   gcloud auth print-identity-token | head -c 80
#   gcloud config get-value auth/impersonate_service_account

set -euo pipefail

: "${OPERATOR_EMAIL:?OPERATOR_EMAIL env var required (your gcloud login)}"
: "${HOST_PROJECT_ID:?HOST_PROJECT_ID env var required (e.g. mtagent-internal-dev)}"

SA_NAME="${SA_NAME:-mtagent-runtime}"
RUNTIME_SA="${SA_NAME}@${HOST_PROJECT_ID}.iam.gserviceaccount.com"

GREEN='\033[0;32m'
NC='\033[0m'
log() { printf "${GREEN}==>${NC} %s\n" "$*"; }

# --- Step 1: Grant operator iam.serviceAccountTokenCreator on the SA ---
# Idempotent; re-running is safe.
log "Granting ${OPERATOR_EMAIL} tokenCreator on ${RUNTIME_SA}"
gcloud iam service-accounts add-iam-policy-binding "${RUNTIME_SA}" \
  --member="user:${OPERATOR_EMAIL}" \
  --role="roles/iam.serviceAccountTokenCreator" \
  --project="${HOST_PROJECT_ID}" \
  --quiet >/dev/null

# --- Step 2: Set gcloud config to impersonate ---
# Affects EVERY subsequent gcloud call in this shell + future shells
# (it's a persistent config setting, not just env). To revert:
# gcloud config unset auth/impersonate_service_account
log "Configuring gcloud to impersonate ${RUNTIME_SA} for all subsequent calls"
gcloud config set auth/impersonate_service_account "${RUNTIME_SA}"

# --- Verify ---
log "Verification: print-identity-token (truncated)"
TOKEN=$(gcloud auth print-identity-token 2>/dev/null | head -c 50 || echo "FAILED")
if [[ "${TOKEN}" == "FAILED" ]] || [[ -z "${TOKEN}" ]]; then
  echo "ERROR: token mint failed. The grant from step 1 may need ~30s"
  echo "to propagate. Re-try in a moment:"
  echo "  gcloud auth print-identity-token | head -c 80"
  exit 1
fi
log "  Token: ${TOKEN}..."

echo
log "Local-dev impersonation configured."
log "Every gcloud call in this shell now runs as: ${RUNTIME_SA}"
echo
log "To revert (run as YOUR account again):"
log "  gcloud config unset auth/impersonate_service_account"
