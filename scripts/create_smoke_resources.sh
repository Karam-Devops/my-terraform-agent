#!/usr/bin/env bash
# Create the resources needed for the Phase 2 SMOKE re-run.
#
# Two waves:
#   * Wave 1 (Phase 1 SMOKE) -- created by hand earlier:
#       poc-vm (Compute Instance)        | poc-subnet (Subnetwork)
#       poc-vpc (Network)                 | poc-fw-allow-icmp (Firewall)
#       poc-disk (Disk)                   | poc-smoke-bucket-... (Bucket)
#       poc-cluster (GKE Autopilot)       | poc-cluster-std (GKE Standard)
#       poc-sa (ServiceAccount)
#   * Wave 2 (Phase 2 SMOKE) -- created by THIS script:
#       poc-keyring (KMS KeyRing)         | poc-key (KMS CryptoKey)
#       poc-cloudrun (Cloud Run v2 svc)   | poc-topic (Pub/Sub Topic)
#       poc-subscription (Pub/Sub Sub)
#
# Total cost while up: ~$0.20-0.25/hr (the GKE clusters from Wave 1
# dominate; Wave 2 resources are essentially free).
#
# Idempotent-friendly: each `gcloud create` exits non-zero if the
# resource already exists, but with `set +e` we continue regardless.
# Re-run is safe.

set +e

PROJECT=dev-proj-470211
REGION=us-central1
LOCATION=us-central1   # KMS uses --location; same value for our smoke

echo "==> Enabling required APIs (idempotent)..."
gcloud services enable cloudkms.googleapis.com run.googleapis.com pubsub.googleapis.com \
  --project="$PROJECT"
echo "    (Wait ~30s for API propagation before first create)"
sleep 30

# ----------------------------------------------------------------------
# P2-3: KMS keyring + crypto key
# ----------------------------------------------------------------------
echo
echo "==> Creating KMS key ring 'poc-keyring' (P2-3)..."
gcloud kms keyrings create poc-keyring \
  --location="$LOCATION" --project="$PROJECT"

echo "==> Creating KMS crypto key 'poc-key' inside 'poc-keyring' (P2-3)..."
gcloud kms keys create poc-key \
  --keyring=poc-keyring --location="$LOCATION" \
  --purpose=encryption --project="$PROJECT"

# ----------------------------------------------------------------------
# P2-4: Cloud Run v2 service (using Google's public hello-world image)
# ----------------------------------------------------------------------
echo
echo "==> Creating Cloud Run v2 service 'poc-cloudrun' (P2-4)..."
echo "    (Uses Google's public hello-world image; ~30-60s deploy time)"
gcloud run deploy poc-cloudrun \
  --image=us-docker.pkg.dev/cloudrun/container/hello \
  --region="$REGION" \
  --no-allow-unauthenticated \
  --project="$PROJECT" \
  --quiet

# ----------------------------------------------------------------------
# P2-5: Pub/Sub topic + subscription
# ----------------------------------------------------------------------
echo
echo "==> Creating Pub/Sub topic 'poc-topic' (P2-5)..."
gcloud pubsub topics create poc-topic --project="$PROJECT"

echo "==> Creating Pub/Sub subscription 'poc-subscription' on 'poc-topic' (P2-5)..."
gcloud pubsub subscriptions create poc-subscription \
  --topic=poc-topic --project="$PROJECT"

# ----------------------------------------------------------------------
# Verify everything is visible to Cloud Asset Inventory.
# Note: Asset API has 1-5 min eventual-consistency lag from create
# time. If something is missing here, wait ~2 min and re-run just
# the verify command at the bottom.
# ----------------------------------------------------------------------
echo
echo "==> Verifying Cloud Asset Inventory has indexed everything..."
echo "    (Asset API has 1-5 min lag; re-run if anything is missing.)"
gcloud asset search-all-resources \
  --scope="projects/$PROJECT" \
  --asset-types=cloudkms.googleapis.com/KeyRing,cloudkms.googleapis.com/CryptoKey,run.googleapis.com/Service,pubsub.googleapis.com/Topic,pubsub.googleapis.com/Subscription \
  --format="value(assetType,name)"

echo
echo "Done. To re-check Asset API later (if any are missing above):"
echo "  gcloud asset search-all-resources \\"
echo "    --scope=projects/$PROJECT \\"
echo "    --asset-types=cloudkms.googleapis.com/KeyRing,cloudkms.googleapis.com/CryptoKey,run.googleapis.com/Service,pubsub.googleapis.com/Topic,pubsub.googleapis.com/Subscription \\"
echo "    --format=\"value(name)\""
echo
echo "When all 5 are visible, re-run the importer SMOKE per"
echo "docs/smoke_test_phase1.md (the picks differ; see the new"
echo "Phase 2 Wave 2 selection guidance to be added in P2-6 if needed)."
