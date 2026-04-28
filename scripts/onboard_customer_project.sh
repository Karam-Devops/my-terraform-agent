#!/usr/bin/env bash
# scripts/onboard_customer_project.sh
#
# Customer-side onboarding: grant mtagent's runtime service account
# read-only access to a customer's GCP project so mtagent can scan
# their infrastructure.
#
# Mirrors Path A from docs/customer_onboarding.md (the recommended
# default per user decision 2026-04-28). Two grants:
#
#   1. roles/viewer on the customer project
#      Single high-level read-only role that covers compute, GKE,
#      storage, KMS, pubsub, IAM viewer, etc. -- everything mtagent
#      needs to enumerate + describe.
#
#   2. roles/iam.serviceAccountTokenCreator on our SA itself
#      Allows our SA to mint impersonation tokens (the cross-project
#      access mechanism).
#
# Plus enables the one hard-required API (cloudasset.googleapis.com).
# Other APIs auto-detected at scan time per CG-11; we never enable
# anything else on the customer's behalf.
#
# Idempotent: re-running on an already-onboarded project is a no-op
# for grants (gcloud silently dedupes binding additions). Safe to run
# multiple times.
#
# Run this:
#   * As a customer GCP admin (owner OR security admin role)
#   * Against the customer's project they want mtagent to scan
#
# Usage:
#   CUSTOMER_PROJECT_ID=acme-prod-12345 \
#   MTAGENT_SA=mtagent-runtime@mtagent-internal-dev.iam.gserviceaccount.com \
#     ./scripts/onboard_customer_project.sh
#
# Env vars:
#   CUSTOMER_PROJECT_ID  (required) GCP project ID to grant access on
#   MTAGENT_SA           (required) Our SA email -- get from us
#   PATH_VARIANT         (optional) "A" (default; roles/viewer) or
#                        "B" (custom role; not implemented yet -- see
#                        docs/customer_onboarding.md Path B for the
#                        manual yaml-based setup)

set -euo pipefail

# --- Inputs ---
: "${CUSTOMER_PROJECT_ID:?CUSTOMER_PROJECT_ID env var required (your GCP project)}"
: "${MTAGENT_SA:?MTAGENT_SA env var required (mtagent's runtime SA email -- ask your mtagent contact)}"
PATH_VARIANT="${PATH_VARIANT:-A}"

if [[ "${PATH_VARIANT}" != "A" ]]; then
  echo "ERROR: only Path A (roles/viewer) is implemented in this script."
  echo "For Path B (custom role with least-privilege permissions), see"
  echo "docs/customer_onboarding.md Path B section -- run the manual"
  echo "gcloud iam roles create + add-iam-policy-binding sequence."
  exit 1
fi

# --- Colors ---
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
RED='\033[0;31m'
NC='\033[0m'

log()  { printf "${GREEN}==>${NC} %s\n" "$*"; }
warn() { printf "${YELLOW}!! ${NC} %s\n" "$*"; }
err()  { printf "${RED}XX ${NC} %s\n" "$*" >&2; }

# --- Pre-flight: confirm operator can see the project ---
log "Pre-flight: verify access to ${CUSTOMER_PROJECT_ID}"
if ! gcloud projects describe "${CUSTOMER_PROJECT_ID}" \
        --format="value(projectId)" >/dev/null 2>&1; then
  err "Cannot describe project ${CUSTOMER_PROJECT_ID}."
  err "Run as a user with at least roles/viewer on this project."
  exit 2
fi

# --- Step 1: Enable Cloud Asset API ---
# This is the ONLY API mtagent strictly requires. Others (compute,
# container, kms, etc.) are auto-detected per CG-11 -- mtagent skips
# any asset type whose API isn't enabled, with a clear notice in
# the inventory.
log "Step 1/2: Enable cloudasset.googleapis.com (the discovery API)"
gcloud services enable cloudasset.googleapis.com \
  --project="${CUSTOMER_PROJECT_ID}"

# --- Step 2: Grant the 2 IAM bindings (Path A) ---
log "Step 2/2: Grant Path A IAM bindings to ${MTAGENT_SA}"

log "  2a. roles/viewer on ${CUSTOMER_PROJECT_ID}"
gcloud projects add-iam-policy-binding "${CUSTOMER_PROJECT_ID}" \
  --member="serviceAccount:${MTAGENT_SA}" \
  --role="roles/viewer" \
  --condition=None \
  --quiet >/dev/null

log "  2b. roles/iam.serviceAccountTokenCreator on the SA itself"
gcloud iam service-accounts add-iam-policy-binding "${MTAGENT_SA}" \
  --member="serviceAccount:${MTAGENT_SA}" \
  --role="roles/iam.serviceAccountTokenCreator" \
  --quiet >/dev/null

# --- Done ---
echo
log "Customer onboarding complete."
echo
log "Summary:"
log "  Customer project: ${CUSTOMER_PROJECT_ID}"
log "  mtagent SA:       ${MTAGENT_SA}"
log "  IAM path:         A (roles/viewer + tokenCreator)"
log "  API enabled:      cloudasset.googleapis.com"
echo
log "Next steps:"
log "  1. Reply to your mtagent contact with the project ID:"
log "       ${CUSTOMER_PROJECT_ID}"
log "  2. They'll verify access from their side via:"
log "       ./scripts/verify_onboarding.sh"
echo
log "To revoke at any time:"
log "  gcloud projects remove-iam-policy-binding ${CUSTOMER_PROJECT_ID} \\"
log "    --member=\"serviceAccount:${MTAGENT_SA}\" \\"
log "    --role=\"roles/viewer\""
