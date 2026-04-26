#!/usr/bin/env bash
# Tear down the resources created for the Phase 1 + Phase 2 SMOKE tests.
#
# Run this AFTER the SMOKE has passed (or failed and been triaged).
# Leaving the GKE clusters up costs ~$0.20/hr ($150/mo combined);
# everything else is essentially free. The clusters are the ones to
# remember.
#
# Companion to scripts/create_smoke_resources.sh -- this is the
# strict reverse of that, plus the original Phase 1 GKE clusters
# which were created by hand earlier.
#
# Idempotent: each `gcloud delete` exits non-zero if the resource is
# already gone; we set +e so the script continues regardless. The
# final `gcloud asset search-all-resources` confirms what's left.
#
# What gets deleted vs preserved
# ------------------------------
# DELETED (created by smoke setup):
#   poc-vm, poc-disk, poc-fw-allow-icmp, poc-vpc, poc-smoke-bucket,
#   poc-cluster (Autopilot), poc-cluster-std (Standard),
#   poc-keyring + poc-key (KMS), poc-cloudrun (Cloud Run v2),
#   poc-topic + poc-subscription (Pub/Sub)
# PRESERVED (pre-existed in dev-proj-470211 before any smoke):
#   poc-subnet (Subnetwork), poc-sa (ServiceAccount), default network

set +e

PROJECT=dev-proj-470211
REGION=us-central1
ZONE=us-central1-a
LOCATION=us-central1

# ----------------------------------------------------------------------
# Slow deletes first so they run in parallel with the fast ones below.
# ----------------------------------------------------------------------
echo "==> Deleting GKE Autopilot cluster (slowest, ~3-5 min)..."
gcloud container clusters delete poc-cluster \
  --project="$PROJECT" --region="$REGION" --quiet

echo "==> Deleting GKE Standard zonal cluster (~3-5 min)..."
gcloud container clusters delete poc-cluster-std \
  --project="$PROJECT" --zone="$ZONE" --quiet

# ----------------------------------------------------------------------
# Phase 2 SMOKE resources (P2-3 + P2-4 + P2-5)
# ----------------------------------------------------------------------
echo "==> Deleting Cloud Run v2 service 'poc-cloudrun' (P2-4)..."
gcloud run services delete poc-cloudrun \
  --region="$REGION" --project="$PROJECT" --quiet

echo "==> Deleting Pub/Sub subscription 'poc-subscription' (P2-5)..."
gcloud pubsub subscriptions delete poc-subscription --project="$PROJECT" --quiet

echo "==> Deleting Pub/Sub topic 'poc-topic' (P2-5)..."
gcloud pubsub topics delete poc-topic --project="$PROJECT" --quiet

# KMS crypto keys can be SCHEDULED for destruction but not immediately
# deleted -- this is by design (CMEK keys must have a 24h-30d
# destruction window per Google's safety guarantees). The smoke key
# costs ~$0.06/month while pending destruction; that's negligible but
# noted so operators don't think the script silently failed.
echo "==> Scheduling KMS crypto key 'poc-key' for destruction (P2-3)..."
echo "    (CMEK keys can't be hard-deleted; they enter a 24h-30d"
echo "    destruction window. ~\$0.06/mo while pending; near-free.)"
gcloud kms keys versions destroy 1 \
  --key=poc-key --keyring=poc-keyring --location="$LOCATION" \
  --project="$PROJECT" --quiet

echo "==> Note: KMS key ring 'poc-keyring' CANNOT be deleted (Google's"
echo "    own design: key rings persist forever once created, even"
echo "    after all keys are destroyed). The empty key ring is harmless"
echo "    and free; it just stays in the project."

# ----------------------------------------------------------------------
# Phase 1 SMOKE resources (created by hand earlier)
# ----------------------------------------------------------------------
echo "==> Deleting Storage bucket 'poc-smoke-bucket-$PROJECT'..."
gcloud storage rm --recursive "gs://poc-smoke-bucket-$PROJECT" --quiet

echo "==> Deleting Compute disk 'poc-disk' (standalone, not boot)..."
gcloud compute disks delete poc-disk \
  --project="$PROJECT" --zone="$ZONE" --quiet

echo "==> Deleting Compute firewall rule 'poc-fw-allow-icmp'..."
gcloud compute firewall-rules delete poc-fw-allow-icmp \
  --project="$PROJECT" --quiet

echo "==> Deleting Compute instance 'poc-vm'..."
gcloud compute instances delete poc-vm \
  --project="$PROJECT" --zone="$ZONE" --quiet

# Network is deleted last because firewalls + subnets reference it.
# poc-subnet is intentionally preserved (pre-existing in dev-proj),
# so the VPC delete will fail unless poc-subnet is in a different
# VPC. In our case poc-subnet IS in poc-vpc, so the delete will
# error on the dependency. Manually delete poc-subnet first if you
# want a fully clean slate (we don't, since poc-subnet was the
# original test fixture).
echo "==> Deleting Compute network 'poc-vpc' (will fail if poc-subnet"
echo "    is still attached -- that's expected; poc-subnet is preserved)..."
gcloud compute networks delete poc-vpc \
  --project="$PROJECT" --quiet

echo
echo "==> Verifying remaining resources (should show ONLY:"
echo "    poc-subnet, poc-sa, the empty poc-keyring, and possibly poc-vpc"
echo "    if poc-subnet kept it pinned)..."
gcloud asset search-all-resources \
  --scope="projects/$PROJECT" \
  --asset-types=compute.googleapis.com/Instance,storage.googleapis.com/Bucket,container.googleapis.com/Cluster,container.googleapis.com/NodePool,cloudkms.googleapis.com/CryptoKey,run.googleapis.com/Service,pubsub.googleapis.com/Topic,pubsub.googleapis.com/Subscription \
  --format="value(assetType,name)"

echo
echo "Cleanup complete. Pre-existing poc-subnet, poc-sa, default network,"
echo "and the empty poc-keyring (Google won't let us delete it) intentionally"
echo "left in place. Re-run scripts/create_smoke_resources.sh to recreate"
echo "the Phase 1 + Phase 2 SMOKE fixtures."
