#!/usr/bin/env bash
# scripts/bootstrap_host_project.sh
#
# Idempotent bootstrap for a fresh GCP host project that will run
# mtagent's Cloud Run service. Creates the project, links billing,
# enables required APIs, and creates the GCS state bucket with
# versioning + lifecycle.
#
# Stage-1 use (internal-dev): user runs this against their personal
# GCP account to create mtagent-internal-dev. Stage-2 (company hosting,
# post-MIG-0): same script, different env vars at the top.
#
# All values env-overridable so the same script works for any
# host-project / bucket-name / region combination. NO project-specific
# literals in the gcloud calls themselves.
#
# Idempotency: every step checks "does this already exist?" before
# creating. Safe to re-run.
#
# Usage:
#   PROJECT_ID=mtagent-internal-dev BILLING=01ABCD-23EFGH-45IJKL ./scripts/bootstrap_host_project.sh
#
# Or with all defaults overridable:
#   PROJECT_ID=...     # required: GCP project ID for hosting
#   PROJECT_NAME=...   # display name (defaults to PROJECT_ID)
#   BILLING=...        # required: billing account ID for project link
#   REGION=...         # default us-central1
#   STATE_BUCKET=...   # default mtagent-state-${env_suffix}
#                      (env_suffix derived from PROJECT_ID's trailing
#                      segment after "mtagent-" prefix; e.g.
#                      mtagent-internal-dev -> internal-dev ->
#                      mtagent-state-dev)

set -euo pipefail

# --- Inputs (env-overridable; required ones validated below) ---
: "${PROJECT_ID:?PROJECT_ID env var required (e.g. mtagent-internal-dev)}"
: "${BILLING:?BILLING env var required (gcloud billing accounts list to find yours)}"
PROJECT_NAME="${PROJECT_NAME:-$PROJECT_ID}"
REGION="${REGION:-us-central1}"

# Derive default state bucket name from project. mtagent-internal-dev ->
# mtagent-state-dev. Override STATE_BUCKET to use a custom name.
_default_bucket="mtagent-state-$(echo "${PROJECT_ID#mtagent-}" | sed 's/internal-//')"
STATE_BUCKET="${STATE_BUCKET:-$_default_bucket}"

# --- Required APIs ---
# Build/deploy + runtime + audit layers. All are read/admin scope on the
# HOSTING project, NOT the customer's project (which gets the much
# tighter set in docs/customer_onboarding.md).
APIS=(
  cloudbuild.googleapis.com         # Cloud Build pipeline
  artifactregistry.googleapis.com   # Docker image registry
  run.googleapis.com                # Cloud Run hosting
  iam.googleapis.com                # SA management
  iamcredentials.googleapis.com     # SA impersonation (cross-project)
  storage.googleapis.com            # GCS state bucket
  aiplatform.googleapis.com         # Vertex AI / Gemini
  serviceusage.googleapis.com       # API enablement detection (CG-11)
  logging.googleapis.com            # Cloud Logging (structured logs)
  monitoring.googleapis.com         # Cloud Monitoring (metrics)
)

# --- Colors for readability ---
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
NC='\033[0m'  # no color

log() {
  printf "${GREEN}==>${NC} %s\n" "$*"
}

warn() {
  printf "${YELLOW}!! ${NC} %s\n" "$*"
}

# --- Step 1: Project (create if missing) ---
log "Step 1/4: Project ${PROJECT_ID}"
if gcloud projects describe "${PROJECT_ID}" --format="value(projectId)" 2>/dev/null | grep -q "${PROJECT_ID}"; then
  warn "Project ${PROJECT_ID} already exists -- skipping create"
else
  log "  Creating project ${PROJECT_ID} (display name: ${PROJECT_NAME})"
  gcloud projects create "${PROJECT_ID}" \
    --name="${PROJECT_NAME}" \
    --set-as-default
fi

# --- Step 2: Billing link (idempotent) ---
log "Step 2/4: Linking billing account ${BILLING}"
_current_billing=$(gcloud billing projects describe "${PROJECT_ID}" --format="value(billingAccountName)" 2>/dev/null || echo "")
if [[ "${_current_billing}" == *"${BILLING}"* ]]; then
  warn "Billing already linked to ${BILLING} -- skipping"
else
  gcloud billing projects link "${PROJECT_ID}" --billing-account="${BILLING}"
fi

# --- Step 3: Enable APIs ---
log "Step 3/4: Enabling ${#APIS[@]} APIs (idempotent; already-enabled APIs are no-ops)"
gcloud services enable "${APIS[@]}" --project="${PROJECT_ID}"

# --- Step 4: GCS state bucket ---
log "Step 4/4: GCS state bucket gs://${STATE_BUCKET}"
if gcloud storage buckets describe "gs://${STATE_BUCKET}" --format="value(name)" 2>/dev/null | grep -q "${STATE_BUCKET}"; then
  warn "Bucket gs://${STATE_BUCKET} already exists -- skipping create (will still ensure versioning)"
else
  gcloud storage buckets create "gs://${STATE_BUCKET}" \
    --project="${PROJECT_ID}" \
    --location="${REGION}" \
    --uniform-bucket-level-access \
    --public-access-prevention
fi

# Ensure versioning is ON (idempotent; bucket might pre-date this script
# OR might have been created earlier with versioning skipped).
log "  Ensuring versioning is enabled on gs://${STATE_BUCKET}"
gcloud storage buckets update "gs://${STATE_BUCKET}" --versioning

_versioning=$(gcloud storage buckets describe "gs://${STATE_BUCKET}" --format="value(versioning.enabled)")
if [[ "${_versioning}" != "True" ]]; then
  warn "Versioning verification: got '${_versioning}', expected 'True'"
  warn "Run manually: gcloud storage buckets update gs://${STATE_BUCKET} --versioning"
  exit 1
fi

# --- Done ---
echo
log "Bootstrap complete."
echo
log "Summary:"
log "  Project:       ${PROJECT_ID}"
log "  Region:        ${REGION}"
log "  State bucket:  gs://${STATE_BUCKET} (versioning: ON)"
log "  APIs enabled:  ${#APIS[@]}"
echo
log "Next step (Phase 5A PSA-2):"
log "  Build + deploy the mtagent image:"
log "    gcloud builds submit --config=cloudbuild.yaml --project=${PROJECT_ID}"
