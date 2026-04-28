#!/usr/bin/env bash
# scripts/list_cloudrun_invokers.sh
#
# PSA-7 (Phase 5A) — Print everyone who can invoke the mtagent
# Cloud Run service.
#
# Audit tool. Use periodically to catch drift (stale teammates,
# overly-broad bindings like allUsers, etc.). Should ALWAYS show
# only the small set of intentionally-allowlisted operator emails
# in Stage 1.
#
# Pre-Stage-2 invariants this audit catches:
#   * NO bindings to "allUsers" or "allAuthenticatedUsers" (would
#     defeat the --no-allow-unauthenticated flag we set at deploy)
#   * NO unexpected service accounts (only the handful we onboard)
#   * NO bindings to wildcards or domains larger than expected
#
# Usage:
#   ./scripts/list_cloudrun_invokers.sh

set -euo pipefail

HOST_PROJECT_ID="${HOST_PROJECT_ID:-mtagent-internal-dev}"
SERVICE_NAME="${SERVICE_NAME:-mtagent-app}"
REGION="${REGION:-us-central1}"

GREEN='\033[0;32m'
YELLOW='\033[0;33m'
RED='\033[0;31m'
NC='\033[0m'
log() { printf "${GREEN}==>${NC} %s\n" "$*"; }

log "Cloud Run invokers for ${SERVICE_NAME} (${HOST_PROJECT_ID}, ${REGION})"
echo

# Pull the IAM policy and grep for the invoker role + its members.
# `--format=json | jq` would be cleaner but jq isn't always available
# on operator machines; the printf gymnastics below work everywhere
# gcloud does.
POLICY=$(gcloud run services get-iam-policy "${SERVICE_NAME}" \
  --region="${REGION}" \
  --project="${HOST_PROJECT_ID}" \
  --format="value(bindings)" 2>/dev/null || echo "")

if [[ -z "${POLICY}" ]]; then
  printf "${YELLOW}!! ${NC} No IAM bindings found, OR you don't have permission to read the policy.\n"
  printf "${YELLOW}!! ${NC} If unexpected, check: gcloud run services get-iam-policy ${SERVICE_NAME} --region=${REGION} --project=${HOST_PROJECT_ID}\n"
  exit 1
fi

# Parse with format that's easier to read (table) directly:
gcloud run services get-iam-policy "${SERVICE_NAME}" \
  --region="${REGION}" \
  --project="${HOST_PROJECT_ID}" \
  --filter="bindings.role:roles/run.invoker" \
  --flatten="bindings[].members" \
  --format="table(bindings.role:label=ROLE,bindings.members:label=MEMBER)"

echo
# Sanity warnings for over-broad bindings
INVOKERS=$(gcloud run services get-iam-policy "${SERVICE_NAME}" \
  --region="${REGION}" \
  --project="${HOST_PROJECT_ID}" \
  --filter="bindings.role:roles/run.invoker" \
  --flatten="bindings[].members" \
  --format="value(bindings.members)")

if echo "${INVOKERS}" | grep -q "allUsers"; then
  printf "${RED}XX ${NC} ALERT: 'allUsers' is in the invoker list -- service is PUBLIC.\n"
  printf "${RED}XX ${NC} Run: ./scripts/revoke_cloudrun_invoker.sh GRANTEE=allUsers\n"
fi

if echo "${INVOKERS}" | grep -q "allAuthenticatedUsers"; then
  printf "${RED}XX ${NC} ALERT: 'allAuthenticatedUsers' is in the invoker list -- ANY Google\n"
  printf "${RED}XX ${NC} account can invoke. Revoke unless this is deliberate.\n"
fi

# Domain-wide bindings (e.g. "domain:example.com") are usually fine but
# worth flagging
if echo "${INVOKERS}" | grep -q "domain:"; then
  printf "${YELLOW}!! ${NC} INFO: domain-wide binding present. Verify it's the\n"
  printf "${YELLOW}!! ${NC}       intended company domain.\n"
fi

log "Audit complete."
log "  To grant:  GRANTEE=email ./scripts/grant_cloudrun_invoker.sh"
log "  To revoke: GRANTEE=email ./scripts/revoke_cloudrun_invoker.sh"
