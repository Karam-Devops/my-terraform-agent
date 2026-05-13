#!/usr/bin/env bash
# scripts/sre_setup_gcp.sh
#
# One-shot, idempotent GCP setup for the SRE / Incident Response Agent
# (Phase 8 Day 1).
#
# What this script does, in order:
#
#   1. Enables the required APIs:
#        pubsub.googleapis.com           — alert transport
#        monitoring.googleapis.com       — Cloud Monitoring alerts
#        cloudasset.googleapis.com       — asset-changes evidence source
#        logging.googleapis.com          — IAM-changes evidence source (audit logs)
#        cloudbuild.googleapis.com       — deploys evidence source
#
#   2. Creates the Pub/Sub topic + pull subscription the agent reads
#      from. The names match sre/triggers/gcp_pubsub.py's defaults:
#        topic         = sre-incident-alerts
#        subscription  = sre-agent-pull-subscription
#      Subscription ack-deadline is set to 300s — gives an operator
#      5 minutes to triage before Pub/Sub redelivers.
#
#   3. Creates (or updates) a Cloud Monitoring notification channel of
#      type 'pubsub' pointing at the topic. Operators can wire this
#      channel into existing alerting policies via the Console or gcloud.
#
#   4. Grants IAM on the runtime SA:
#        roles/pubsub.subscriber     — pull from the subscription
#        roles/monitoring.viewer     — read alert policies (Day 2)
#        roles/cloudasset.viewer     — Asset Inventory feeds (Day 2)
#        roles/logging.viewer        — audit-log reads (Day 2)
#        roles/cloudbuild.builds.viewer — deploy history (Day 2)
#
# Idempotent: every step uses `gcloud ... describe ... || gcloud ... create ...`
# so re-running on an already-set-up project is a no-op. Safe to run
# multiple times — useful when iterating during development.
#
# Usage:
#   PROJECT_ID=dev-proj-470211 \
#   AGENT_SA=mtagent-runtime@dev-proj-470211.iam.gserviceaccount.com \
#     ./scripts/sre_setup_gcp.sh
#
# Env vars:
#   PROJECT_ID            (required) GCP project hosting the agent + alerts
#   AGENT_SA              (required) Runtime SA email to grant IAM to.
#                                   For Cloud Run, this is the service
#                                   identity of the SRE Agent service.
#   TOPIC_NAME            (optional) Pub/Sub topic name (default:
#                                   sre-incident-alerts)
#   SUBSCRIPTION_NAME     (optional) Pull subscription name (default:
#                                   sre-agent-pull-subscription)
#   NOTIFICATION_CHANNEL_DISPLAY_NAME (optional) Cloud Monitoring channel
#                                   display name (default: "SRE Agent
#                                   Pub/Sub Channel")
#   ACK_DEADLINE_SECONDS  (optional) Pub/Sub ack deadline (default: 300)
#
# Exit codes:
#   0 — success (or already-set-up, no-op)
#   1 — missing required env var, or a gcloud step failed unrecoverably
#
# Re: granting roles on the project (not on individual resources):
#   pubsub.subscriber CAN be granted on the subscription resource alone
#   for tighter scope, but the other 4 roles only exist at project level
#   anyway, so a single project-level binding keeps the script simple.
#   Phase 1 hardening: tighten pubsub.subscriber to the resource scope.

set -euo pipefail

# --- Inputs ---
: "${PROJECT_ID:?PROJECT_ID env var required (GCP project hosting the agent)}"
: "${AGENT_SA:?AGENT_SA env var required (runtime SA email)}"

TOPIC_NAME="${TOPIC_NAME:-sre-incident-alerts}"
SUBSCRIPTION_NAME="${SUBSCRIPTION_NAME:-sre-agent-pull-subscription}"
NOTIFICATION_CHANNEL_DISPLAY_NAME="${NOTIFICATION_CHANNEL_DISPLAY_NAME:-SRE Agent Pub/Sub Channel}"
ACK_DEADLINE_SECONDS="${ACK_DEADLINE_SECONDS:-300}"

# --- Colors ---
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

log()  { echo -e "${BLUE}[sre-setup]${NC} $*"; }
ok()   { echo -e "${GREEN}[ ok ]${NC} $*"; }
warn() { echo -e "${YELLOW}[warn]${NC} $*"; }
err()  { echo -e "${RED}[fail]${NC} $*"; }

log "Project:          ${PROJECT_ID}"
log "Agent SA:         ${AGENT_SA}"
log "Topic:            ${TOPIC_NAME}"
log "Subscription:     ${SUBSCRIPTION_NAME}"
log "Ack deadline:     ${ACK_DEADLINE_SECONDS}s"
echo

# ---------------------------------------------------------------------------
# Step 1: enable APIs.
#
# `gcloud services enable` is idempotent and batched, so one call is
# safe even when some APIs are already on. Each enable can take 30-60s
# on a cold project, but completes near-instantly on subsequent runs.
# ---------------------------------------------------------------------------

log "Step 1/4: enabling required APIs…"
gcloud services enable \
    pubsub.googleapis.com \
    monitoring.googleapis.com \
    cloudasset.googleapis.com \
    logging.googleapis.com \
    cloudbuild.googleapis.com \
    --project="${PROJECT_ID}"
ok "APIs enabled."
echo

# ---------------------------------------------------------------------------
# Step 2: Pub/Sub topic + pull subscription.
#
# Topic first (subscription needs it as a parent). Both are guarded by
# a `describe` check so re-runs don't error on "already exists".
# ---------------------------------------------------------------------------

log "Step 2/4: Pub/Sub topic + subscription…"

if gcloud pubsub topics describe "${TOPIC_NAME}" \
    --project="${PROJECT_ID}" >/dev/null 2>&1; then
    ok "Topic ${TOPIC_NAME} already exists."
else
    gcloud pubsub topics create "${TOPIC_NAME}" \
        --project="${PROJECT_ID}"
    ok "Created topic ${TOPIC_NAME}."
fi

if gcloud pubsub subscriptions describe "${SUBSCRIPTION_NAME}" \
    --project="${PROJECT_ID}" >/dev/null 2>&1; then
    ok "Subscription ${SUBSCRIPTION_NAME} already exists."
    # Idempotent update: keep ack-deadline aligned with the script's
    # expected value. Useful when an earlier run used a different
    # default — the next setup pass converges it.
    gcloud pubsub subscriptions update "${SUBSCRIPTION_NAME}" \
        --project="${PROJECT_ID}" \
        --ack-deadline="${ACK_DEADLINE_SECONDS}" >/dev/null
else
    gcloud pubsub subscriptions create "${SUBSCRIPTION_NAME}" \
        --topic="${TOPIC_NAME}" \
        --ack-deadline="${ACK_DEADLINE_SECONDS}" \
        --project="${PROJECT_ID}"
    ok "Created subscription ${SUBSCRIPTION_NAME} (ack-deadline ${ACK_DEADLINE_SECONDS}s)."
fi
echo

# ---------------------------------------------------------------------------
# Step 3: Cloud Monitoring notification channel.
#
# A pubsub-type channel routes alert policy notifications to a topic.
# Operators attach this channel to whichever alerting policies they
# want the SRE agent to triage.
#
# `gcloud alpha monitoring channels` is the official path today. The
# describe-then-create idiom doesn't fit channels well (channels are
# identified by an auto-generated name, not display_name), so instead
# we list existing pubsub channels and check if any already points at
# our topic. If yes → reuse. If no → create.
# ---------------------------------------------------------------------------

log "Step 3/4: Cloud Monitoring notification channel…"

TOPIC_FQN="projects/${PROJECT_ID}/topics/${TOPIC_NAME}"

# Find any existing pubsub channel pointing at our topic.
EXISTING_CHANNEL=$(gcloud alpha monitoring channels list \
    --project="${PROJECT_ID}" \
    --filter="type=pubsub AND labels.topic=${TOPIC_FQN}" \
    --format="value(name)" 2>/dev/null | head -n 1 || true)

if [[ -n "${EXISTING_CHANNEL}" ]]; then
    ok "Notification channel already points at ${TOPIC_FQN}:"
    echo "       ${EXISTING_CHANNEL}"
else
    # Write a tiny channel-config JSON to a temp file because the gcloud
    # alpha command takes --channel-content-from-file.
    TMP_CHANNEL_JSON="$(mktemp)"
    trap 'rm -f "${TMP_CHANNEL_JSON}"' EXIT
    cat > "${TMP_CHANNEL_JSON}" <<EOF
{
  "type": "pubsub",
  "displayName": "${NOTIFICATION_CHANNEL_DISPLAY_NAME}",
  "labels": {
    "topic": "${TOPIC_FQN}"
  }
}
EOF
    NEW_CHANNEL=$(gcloud alpha monitoring channels create \
        --project="${PROJECT_ID}" \
        --channel-content-from-file="${TMP_CHANNEL_JSON}" \
        --format="value(name)")
    ok "Created notification channel ${NEW_CHANNEL}"
fi
echo

# ---------------------------------------------------------------------------
# Step 4: IAM grants on the runtime SA.
#
# All grants at the project level. `gcloud projects add-iam-policy-binding`
# is idempotent — re-running with the same member+role is a no-op.
# ---------------------------------------------------------------------------

log "Step 4/4: granting IAM roles to ${AGENT_SA}…"

ROLES=(
    "roles/pubsub.subscriber"
    "roles/monitoring.viewer"
    "roles/cloudasset.viewer"
    "roles/logging.viewer"
    "roles/cloudbuild.builds.viewer"
)

for role in "${ROLES[@]}"; do
    gcloud projects add-iam-policy-binding "${PROJECT_ID}" \
        --member="serviceAccount:${AGENT_SA}" \
        --role="${role}" \
        --condition=None \
        --quiet >/dev/null
    ok "Granted ${role}"
done

echo
log "Done. Next steps:"
echo "  1. Attach the notification channel to your Cloud Monitoring alerting"
echo "     policies (Console → Monitoring → Alerting → Edit policy →"
echo "     Notifications → add the '${NOTIFICATION_CHANNEL_DISPLAY_NAME}' channel)."
echo "  2. Seed demo alerts:  python scripts/sre_seed_demo_alerts.py \\"
echo "                          --project=${PROJECT_ID} --topic=${TOPIC_NAME}"
echo "  3. Open the SRE Agent page in the mtagent Streamlit UI and click"
echo "     'Pull now' to verify the queue receives messages."
