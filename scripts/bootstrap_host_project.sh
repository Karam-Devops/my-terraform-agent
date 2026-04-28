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
log "Step 4/6: GCS state bucket gs://${STATE_BUCKET}"
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

# --- Step 5: Artifact Registry repo (for Cloud Build → Cloud Run image push) ---
ARTIFACT_REPO="${ARTIFACT_REPO:-mtagent}"
log "Step 5/6: Artifact Registry repo '${ARTIFACT_REPO}' in ${REGION}"
if gcloud artifacts repositories describe "${ARTIFACT_REPO}" \
        --location="${REGION}" --project="${PROJECT_ID}" \
        --format="value(name)" 2>/dev/null | grep -q "${ARTIFACT_REPO}"; then
  warn "Artifact Registry repo '${ARTIFACT_REPO}' already exists -- skipping create"
else
  gcloud artifacts repositories create "${ARTIFACT_REPO}" \
    --repository-format=docker \
    --location="${REGION}" \
    --project="${PROJECT_ID}" \
    --description="mtagent container images (Cloud Run)"
fi

# --- Step 6: Runtime service account (Cloud Run identity) ---
# This is the SA that the Cloud Run service runs as. It needs:
#   - aiplatform.user: call Vertex AI Gemini
#   - storage.objectAdmin (scoped to MTAGENT_STATE_BUCKET): r/w state
#   - iam.serviceAccountTokenCreator on ITSELF: cross-project SA
#     impersonation (the mechanism for scanning customer projects)
#   - logging.logWriter + monitoring.metricWriter: observability
#
# We do NOT bind cloudasset.viewer / compute.viewer / etc. on the
# HOSTING project here -- those live on the CUSTOMER project (granted
# by the customer per docs/customer_onboarding.md), and our runtime
# SA reaches them via cross-project impersonation.

RUNTIME_SA_NAME="mtagent-runtime"
RUNTIME_SA_EMAIL="${RUNTIME_SA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"
log "Step 6/6: Runtime service account ${RUNTIME_SA_EMAIL}"
if gcloud iam service-accounts describe "${RUNTIME_SA_EMAIL}" \
        --project="${PROJECT_ID}" \
        --format="value(email)" 2>/dev/null | grep -q "${RUNTIME_SA_EMAIL}"; then
  warn "Runtime SA ${RUNTIME_SA_EMAIL} already exists -- skipping create"
else
  gcloud iam service-accounts create "${RUNTIME_SA_NAME}" \
    --display-name="mtagent Cloud Run runtime" \
    --description="Identity for the mtagent Cloud Run service. Needs Vertex AI + GCS state-bucket + cross-project impersonation." \
    --project="${PROJECT_ID}"
fi

log "  Granting hosting-project roles to ${RUNTIME_SA_EMAIL}"
for role in \
    roles/aiplatform.user \
    roles/logging.logWriter \
    roles/monitoring.metricWriter \
    roles/iam.serviceAccountTokenCreator; do
  gcloud projects add-iam-policy-binding "${PROJECT_ID}" \
    --member="serviceAccount:${RUNTIME_SA_EMAIL}" \
    --role="${role}" \
    --condition=None \
    --quiet >/dev/null
done

# Bucket-scoped role (more precise than project-wide storage.admin)
log "  Granting bucket-scoped storage.objectAdmin on gs://${STATE_BUCKET}"
gcloud storage buckets add-iam-policy-binding "gs://${STATE_BUCKET}" \
  --member="serviceAccount:${RUNTIME_SA_EMAIL}" \
  --role=roles/storage.objectAdmin >/dev/null

# Self-impersonation (allow our SA to mint tokens for itself; this is
# what enables the cross-project SA impersonation pattern downstream)
log "  Granting self-impersonation (serviceAccountTokenCreator on self)"
gcloud iam service-accounts add-iam-policy-binding "${RUNTIME_SA_EMAIL}" \
  --member="serviceAccount:${RUNTIME_SA_EMAIL}" \
  --role=roles/iam.serviceAccountTokenCreator \
  --project="${PROJECT_ID}" >/dev/null

# --- Done ---
echo
log "Bootstrap complete."
echo
log "Summary:"
log "  Project:           ${PROJECT_ID}"
log "  Region:            ${REGION}"
log "  State bucket:      gs://${STATE_BUCKET} (versioning: ON)"
log "  APIs enabled:      ${#APIS[@]}"
log "  Artifact registry: ${REGION}-docker.pkg.dev/${PROJECT_ID}/${ARTIFACT_REPO}"
log "  Runtime SA:        ${RUNTIME_SA_EMAIL}"
echo
log "Next step (Phase 5A PSA-2):"
log "  Build + deploy the mtagent image:"
log "    gcloud builds submit --config=cloudbuild.yaml --project=${PROJECT_ID}"
echo
log "After deploy, the Cloud Run URL is invoker-restricted (PSA-7);"
log "grant your operator email run.invoker:"
log "    gcloud run services add-iam-policy-binding mtagent-app \\"
log "      --member='user:YOUR_EMAIL@example.com' \\"
log "      --role=roles/run.invoker \\"
log "      --region=${REGION} \\"
log "      --project=${PROJECT_ID}"
