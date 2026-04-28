#!/usr/bin/env bash
# scripts/verify_onboarding.sh
#
# Operator-side verification: confirm mtagent's runtime SA can
# actually scan a customer project. Run this AFTER the customer has
# completed scripts/onboard_customer_project.sh on their side.
#
# Three checks:
#   1. The runtime SA can call gcloud asset search-all-resources
#      against the customer project (proves the
#      cloudasset.googleapis.com + roles/viewer wiring works).
#   2. The runtime SA can describe at least one resource type
#      (proves the per-resource describe path works -- not just
#      enumeration).
#   3. The runtime SA does NOT have write permissions (sanity
#      check; if this surfaces a write capability, something in
#      the IAM grant is over-privileged).
#
# Usage:
#   CUSTOMER_PROJECT_ID=acme-prod-12345 \
#   MTAGENT_SA=mtagent-runtime@mtagent-internal-dev.iam.gserviceaccount.com \
#     ./scripts/verify_onboarding.sh
#
# This script must be run by an operator who has tokenCreator on the
# mtagent SA (so they can mint impersonation tokens to act AS the SA).
# In Cloud Run production, the runtime SA acts as itself directly -- no
# impersonation needed; this script's --impersonate flag is the local-
# dev / verification-from-laptop equivalent.

set -euo pipefail

: "${CUSTOMER_PROJECT_ID:?CUSTOMER_PROJECT_ID env var required}"
: "${MTAGENT_SA:?MTAGENT_SA env var required}"

GREEN='\033[0;32m'
YELLOW='\033[0;33m'
RED='\033[0;31m'
NC='\033[0m'

log()  { printf "${GREEN}==>${NC} %s\n" "$*"; }
warn() { printf "${YELLOW}!! ${NC} %s\n" "$*"; }
err()  { printf "${RED}XX ${NC} %s\n" "$*" >&2; }

# --- Check 1: Cloud Asset enumeration via impersonation ---
log "Check 1/3: Cloud Asset enumeration as ${MTAGENT_SA}"
COUNT=$(gcloud asset search-all-resources \
  --scope="projects/${CUSTOMER_PROJECT_ID}" \
  --asset-types="storage.googleapis.com/Bucket" \
  --impersonate-service-account="${MTAGENT_SA}" \
  --format="value(name)" 2>/dev/null | wc -l || echo "ERROR")

if [[ "${COUNT}" == "ERROR" ]]; then
  err "Cloud Asset enumeration FAILED."
  err "Likely causes:"
  err "  - cloudasset.googleapis.com not enabled in ${CUSTOMER_PROJECT_ID}"
  err "  - roles/viewer not granted to ${MTAGENT_SA}"
  err "  - You don't have iam.serviceAccountTokenCreator on ${MTAGENT_SA}"
  exit 3
fi
log "  Found ${COUNT} storage bucket(s) -- enumeration works"

# --- Check 2: Per-resource describe ---
log "Check 2/3: gcloud project describe (any read-only call)"
PROJECT_NAME=$(gcloud projects describe "${CUSTOMER_PROJECT_ID}" \
  --impersonate-service-account="${MTAGENT_SA}" \
  --format="value(name)" 2>/dev/null || echo "ERROR")

if [[ "${PROJECT_NAME}" == "ERROR" ]] || [[ -z "${PROJECT_NAME}" ]]; then
  err "Per-resource describe FAILED. Check the SA has roles/viewer."
  exit 4
fi
log "  Project name: ${PROJECT_NAME}"

# --- Check 3: Negative case (sanity: no write permissions) ---
# Try a write op that SHOULD fail. We try creating a fake bucket;
# expect permission-denied. If it succeeds, the SA is over-privileged.
log "Check 3/3: Sanity check -- write ops MUST fail (no overprivilege)"
FAKE_BUCKET="mtagent-verify-write-test-$(date +%s)"
WRITE_RESULT=$(gcloud storage buckets create "gs://${FAKE_BUCKET}" \
  --project="${CUSTOMER_PROJECT_ID}" \
  --impersonate-service-account="${MTAGENT_SA}" \
  --location=us-central1 2>&1 || echo "EXPECTED_FAILURE")

if [[ "${WRITE_RESULT}" == *"EXPECTED_FAILURE"* ]] \
        || [[ "${WRITE_RESULT}" == *"permission"* ]] \
        || [[ "${WRITE_RESULT}" == *"403"* ]] \
        || [[ "${WRITE_RESULT}" == *"denied"* ]]; then
  log "  Write op rejected as expected (read-only confirmed)"
else
  err "WARN: write op did NOT fail. SA may be over-privileged."
  err "Output:"
  err "${WRITE_RESULT}"
  # Cleanup the bucket if it actually got created
  gcloud storage buckets delete "gs://${FAKE_BUCKET}" \
    --project="${CUSTOMER_PROJECT_ID}" \
    --impersonate-service-account="${MTAGENT_SA}" --quiet 2>/dev/null || true
  exit 5
fi

# --- Done ---
echo
log "Verification PASS: ${MTAGENT_SA} can scan ${CUSTOMER_PROJECT_ID}"
log "(read-only confirmed -- no write capability detected)"
echo
log "Customer ${CUSTOMER_PROJECT_ID} is now scannable from mtagent."
