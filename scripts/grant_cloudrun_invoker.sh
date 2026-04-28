#!/usr/bin/env bash
# scripts/grant_cloudrun_invoker.sh
#
# PSA-7 (Phase 5A) — Grant a user the run.invoker role on the
# mtagent Cloud Run service.
#
# Stage 1 auth model: the Cloud Run service deployed by PSA-2 is
# `--no-allow-unauthenticated`. To actually hit the URL (or proxy it
# via `gcloud run services proxy`), an account needs the
# `roles/run.invoker` role on the service.
#
# Round-1 (current): operator + occasional teammate (allowlisted
# individually). Stage 2 adds IAP (PSA-7b) which replaces this
# per-email model with an IAP-managed allowlist.
#
# Idempotent: re-granting an existing binding is a no-op (gcloud
# silently dedupes).
#
# Usage:
#   GRANTEE=teammate@example.com ./scripts/grant_cloudrun_invoker.sh
#
# Env vars:
#   GRANTEE              (required) Email to grant invoker access to
#   HOST_PROJECT_ID      (default mtagent-internal-dev)
#   SERVICE_NAME         (default mtagent-app)
#   REGION               (default us-central1)

set -euo pipefail

: "${GRANTEE:?GRANTEE env var required (email to grant invoker access)}"
HOST_PROJECT_ID="${HOST_PROJECT_ID:-mtagent-internal-dev}"
SERVICE_NAME="${SERVICE_NAME:-mtagent-app}"
REGION="${REGION:-us-central1}"

# Detect grantee type: "user:" for human accounts, "serviceAccount:"
# for SAs (e.g. CI runners). Heuristic: if it looks like an SA email,
# use serviceAccount: prefix.
if [[ "${GRANTEE}" == *.iam.gserviceaccount.com ]]; then
  MEMBER="serviceAccount:${GRANTEE}"
else
  MEMBER="user:${GRANTEE}"
fi

GREEN='\033[0;32m'
NC='\033[0m'
log() { printf "${GREEN}==>${NC} %s\n" "$*"; }

log "Granting roles/run.invoker on ${SERVICE_NAME} to ${MEMBER}"
gcloud run services add-iam-policy-binding "${SERVICE_NAME}" \
  --member="${MEMBER}" \
  --role="roles/run.invoker" \
  --region="${REGION}" \
  --project="${HOST_PROJECT_ID}" \
  --quiet >/dev/null

echo
log "Done. ${GRANTEE} can now access the service via:"
log "  Authenticated browser request (with bearer identity token), OR"
log "  gcloud run services proxy ${SERVICE_NAME} \\"
log "    --port=8080 --region=${REGION} --project=${HOST_PROJECT_ID}"
echo
log "To audit current invoker list:"
log "  ./scripts/list_cloudrun_invokers.sh"
echo
log "To revoke:"
log "  GRANTEE=${GRANTEE} ./scripts/revoke_cloudrun_invoker.sh"
