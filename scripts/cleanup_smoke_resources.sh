#!/usr/bin/env bash
# Tear down the resources created for the Phase 1 SMOKE test.
#
# Run this AFTER the SMOKE has passed (or failed and been triaged).
# Leaving the GKE cluster up costs ~$0.10/hr ($73/mo) -- the others
# are free or near-free, but the cluster is the one to remember.
#
# Idempotent: each `gcloud delete` exits non-zero if the resource is
# already gone; we set +e so the script continues regardless. The
# final `gcloud asset search-all-resources` confirms what's left.

set +e

PROJECT=dev-proj-470211
REGION=us-central1
ZONE=us-central1-a

echo "==> Deleting GKE Autopilot cluster (slowest, ~3-5 min)..."
gcloud container clusters delete poc-cluster \
  --project="$PROJECT" --region="$REGION" --quiet

echo "==> Deleting GKE Standard zonal cluster (~3-5 min)..."
gcloud container clusters delete poc-cluster-std \
  --project="$PROJECT" --zone="$ZONE" --quiet

echo "==> Deleting Storage bucket (must empty first if non-empty)..."
gcloud storage rm --recursive "gs://poc-smoke-bucket-$PROJECT" --quiet

echo "==> Deleting Compute instance..."
gcloud compute instances delete poc-vm \
  --project="$PROJECT" --zone="$ZONE" --quiet

echo
echo "==> Verifying remaining smoke resources (should show only the"
echo "    pre-existing poc-subnet and poc-sa)..."
gcloud asset search-all-resources \
  --scope="projects/$PROJECT" \
  --asset-types=compute.googleapis.com/Instance,storage.googleapis.com/Bucket,container.googleapis.com/Cluster,container.googleapis.com/NodePool \
  --format="value(assetType,name)"

echo
echo "Cleanup complete. Pre-existing poc-subnet (compute_subnetwork)"
echo "and poc-sa (service_account) intentionally left in place."
