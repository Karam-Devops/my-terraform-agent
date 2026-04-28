#!/usr/bin/env bash
# scripts/revoke_cloudrun_invoker.sh
#
# PSA-7 (Phase 5A) — Revoke run.invoker from a user on the mtagent
# Cloud Run service. Pair with grant_cloudrun_invoker.sh.
#
# Use when:
#   * A teammate leaves
#   * Auditing surfaces an account that shouldn't have access
#   * Switching from per-email IAM (Stage 1) to IAP allowlist (Stage 2)
#     -- after IAP is wired, revoke all the per-email invokers
#
# Idempotent: removing an already-absent binding is a no-op (gcloud
# returns 0 silently).
#
# Usage:
#   GRANTEE=former-teammate@example.com ./scripts/revoke_cloudrun_invoker.sh

set -euo pipefail

: "${GRANTEE:?GRANTEE env var required (email to revoke invoker access from)}"
HOST_PROJECT_ID="${HOST_PROJECT_ID:-mtagent-internal-dev}"
SERVICE_NAME="${SERVICE_NAME:-mtagent-app}"
REGION="${REGION:-us-central1}"

if [[ "${GRANTEE}" == *.iam.gserviceaccount.com ]]; then
  MEMBER="serviceAccount:${GRANTEE}"
else
  MEMBER="user:${GRANTEE}"
fi

GREEN='\033[0;32m'
NC='\033[0m'
log() { printf "${GREEN}==>${NC} %s\n" "$*"; }

log "Revoking roles/run.invoker on ${SERVICE_NAME} from ${MEMBER}"
gcloud run services remove-iam-policy-binding "${SERVICE_NAME}" \
  --member="${MEMBER}" \
  --role="roles/run.invoker" \
  --region="${REGION}" \
  --project="${HOST_PROJECT_ID}" \
  --quiet >/dev/null

echo
log "Done. ${GRANTEE} no longer has invoker access."
log "  Effect is immediate -- next request from this account returns 403."
echo
log "To audit current invoker list:"
log "  ./scripts/list_cloudrun_invokers.sh"
