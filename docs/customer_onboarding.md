# Adding Your GCP Project to mtagent

**Estimated time:** 5-10 minutes (one of your GCP admins).
**Ongoing access:** Read-only. mtagent never writes to your project.
**Credentials exchanged:** None. Auth is via Google's cross-project
service-account impersonation — no keys, no secrets.

---

## Quick overview

mtagent is a SaaS that imports your GCP infrastructure into Terraform,
translates it to AWS/Azure if needed, detects drift, and runs policy
compliance checks. It runs in **our** GCP project. To scan **your**
project, you grant our service account read-only access on your side.

---

## What you need to do

### Step 1: Send us your GCP project ID

Looks like `my-company-prod-12345`. Find it via
`gcloud config get-value project` or in the GCP Console.

### Step 2: Enable ONE API in your project

Only one API is hard-required:

```bash
gcloud services enable cloudasset.googleapis.com \
  --project=YOUR_PROJECT_ID
```

That's the discovery API — it's how mtagent enumerates which
resource types you have. Without it we can't see anything.

**About the other APIs:** mtagent auto-detects what you have enabled.
Whatever you have enabled, we scan. Whatever you don't, we skip + show
clearly in the inventory:

```
Inventory: 17 asset types attempted
  ✅ 13 types scanned (compute, GKE, KMS, Pub/Sub, ...)
  ⚠️  4 types not scanned (APIs not enabled in your project):
      - run.googleapis.com (Cloud Run not in use)
      - sqladmin.googleapis.com (Cloud SQL not in use)
      - ...
```

If you later want a skipped type scanned, you enable that API yourself
— mtagent never enables APIs on your behalf.

### Step 3: Grant our service account access (pick ONE path)

Our service account is:
**`mtagent-runtime@<our-host-project>.iam.gserviceaccount.com`**
*(we'll send the exact email when you confirm onboarding)*

Pick the path that fits your security posture:

#### Path A — Quick setup (most customers, ~30 seconds)

Single high-level role + the impersonation grant + the one required API.

**Easiest option — run our scripted version:**

```bash
# Download the script (or get it from your mtagent contact)
# Then:
CUSTOMER_PROJECT_ID=YOUR_PROJECT_ID \
MTAGENT_SA=mtagent-runtime@<our-host-project>.iam.gserviceaccount.com \
  ./onboard_customer_project.sh
```

The script is idempotent (safe to re-run), prints a summary, and
includes the revoke commands at the end for your records.

**Manual equivalent (if you want to inspect each step):**

```bash
PROJECT=YOUR_PROJECT_ID
SA=mtagent-runtime@<our-host-project>.iam.gserviceaccount.com

# Enable the one required API (Cloud Asset; for resource discovery)
gcloud services enable cloudasset.googleapis.com --project=$PROJECT

# Grant project-wide read access (covers compute, GKE, storage, KMS,
# pubsub, IAM viewer, etc. -- all the read-only verbs we need)
gcloud projects add-iam-policy-binding $PROJECT \
  --member="serviceAccount:$SA" \
  --role="roles/viewer"

# Allow our SA to impersonate (the cross-project auth mechanism)
gcloud iam service-accounts add-iam-policy-binding $SA \
  --member="serviceAccount:$SA" \
  --role="roles/iam.serviceAccountTokenCreator" \
  --project=YOUR_PROJECT_ID
```

`roles/viewer` is GCP's standard project-level read-only role. Two
grants + one API. Done.

#### Path B — Least privilege (security-conscious customers, ~5 minutes)

If your security team disallows `roles/viewer` (e.g. you don't want us
seeing audit logs or billing), use a custom role with only the
permissions mtagent actually calls. Save this as `mtagent-role.yaml`:

```yaml
title: mtagent Read-Only Scanner
description: Permissions used by mtagent for read-only resource discovery
            and describe calls. No write, no IAM mutation, no billing.
stage: GA
includedPermissions:
  # Discovery (CloudAsset)
  - cloudasset.assets.searchAllResources

  # Compute (instances, disks, networks, firewalls, subnets, addresses,
  # instance templates)
  - compute.instances.get
  - compute.disks.get
  - compute.networks.get
  - compute.firewalls.get
  - compute.subnetworks.get
  - compute.addresses.get
  - compute.instanceTemplates.get

  # GKE (clusters + node pools)
  - container.clusters.get
  - container.nodes.get

  # IAM (service accounts only; not the broader IAM policy surface)
  - iam.serviceAccounts.get

  # Storage
  - storage.buckets.get

  # KMS
  - cloudkms.keyRings.get
  - cloudkms.cryptoKeys.get

  # Pub/Sub
  - pubsub.topics.get
  - pubsub.subscriptions.get

  # Cloud Run
  - run.services.get

  # Cloud SQL (when scoped in)
  - cloudsql.instances.get

  # API-enablement detection (CG-11) — lets mtagent surface clearly
  # which asset types you have disabled
  - serviceusage.services.list
```

Then grant:

```bash
PROJECT=YOUR_PROJECT_ID
SA=mtagent-runtime@<our-host-project>.iam.gserviceaccount.com

# Create the custom role
gcloud iam roles create mtagentReadOnlyScanner \
  --project=$PROJECT \
  --file=mtagent-role.yaml

# Bind it to our SA
gcloud projects add-iam-policy-binding $PROJECT \
  --member="serviceAccount:$SA" \
  --role="projects/$PROJECT/roles/mtagentReadOnlyScanner"

# Impersonation grant (same as Path A)
gcloud iam service-accounts add-iam-policy-binding $SA \
  --member="serviceAccount:$SA" \
  --role="roles/iam.serviceAccountTokenCreator" \
  --project=$PROJECT
```

### Step 4: Confirm completion

Send us:
- Your GCP project ID
- Which path you chose (A or B)
- (Optional) Output of `gcloud projects get-iam-policy YOUR_PROJECT_ID --filter='bindings.members:mtagent-runtime'` so we can verify

We'll confirm your project is live in the mtagent dashboard within
1 business day.

---

## What we will NOT have

For your audit / security team:

- ❌ **No write access to ANY resource.** Path A uses `roles/viewer`
  (read-only by definition); Path B is a custom role with only `*.get`
  / `*.list` / `*.searchAllResources` permissions.
- ❌ **No service-account key files.** Auth is via Google's
  cross-project SA impersonation — keyless. Every action is logged in
  your Cloud Audit Logs under our SA email.
- ❌ **No automatic API enablement.** If you've disabled an API
  deliberately, mtagent reports "not scanned" rather than enabling it.
- ❌ **No data leaves your control plane silently.** Resource snapshots
  are fetched into our Cloud Run instance, processed, and shown to
  you in the UI. We retain only the per-tenant workdir for your
  ongoing dashboard view; no long-term replication elsewhere.
- ❌ **(Path B only) No visibility into your billing, audit logs, IAM
  policies, KMS material, organization metadata, or anything else
  not in the explicit permissions list above.**

---

## Verifying our access in your audit logs

Every mtagent action shows in Cloud Audit Logs under our SA email.
Filter:

```
protoPayload.authenticationInfo.principalEmail="mtagent-runtime@<our-host-project>.iam.gserviceaccount.com"
```

You'll see only `read`, `list`, `describe`, `get`, and
`searchAllResources` calls. Zero mutations.

---

## Revoking access

To remove mtagent's access at any time:

**Path A:**

```bash
gcloud projects remove-iam-policy-binding YOUR_PROJECT_ID \
  --member="serviceAccount:mtagent-runtime@<our-host-project>.iam.gserviceaccount.com" \
  --role="roles/viewer"
```

**Path B:**

```bash
gcloud projects remove-iam-policy-binding YOUR_PROJECT_ID \
  --member="serviceAccount:mtagent-runtime@<our-host-project>.iam.gserviceaccount.com" \
  --role="projects/YOUR_PROJECT_ID/roles/mtagentReadOnlyScanner"

gcloud iam roles delete mtagentReadOnlyScanner --project=YOUR_PROJECT_ID
```

Effect is immediate. mtagent stops being able to scan; historical
snapshots in our system can be deleted on request.

---

## FAQ

**Q: Can we scope access to specific labels / resource groups only?**
A: Yes, via IAM Conditions. Tell us which labels (e.g.
`scanned_by_mtagent=true`) and we'll send a label-conditioned binding
script.

**Q: Can we use a folder-level grant instead of project-level?**
A: Yes. Replace `gcloud projects add-iam-policy-binding $PROJECT`
with `gcloud resource-manager folders add-iam-policy-binding $FOLDER_ID`.
Inheritance handles the rest.

**Q: Do you need billing account info, org ID, or folder ID?**
A: No. Project ID alone is sufficient. We do not enumerate above the
project level.

**Q: How often does mtagent scan?**
A: On-demand only — you (or we) trigger scans manually from the UI.
No background polling until you explicitly opt in via Settings.

**Q: What if we have resources in resource types you don't yet support?**
A: Those are silently ignored. We'll add support for new types based
on customer demand. Tell us what you need; we add the discovery
mapping (~1-day per type).

**Q: Can my team see mtagent's running activity in real time?**
A: Yes — Cloud Audit Logs are real-time. Add a Log Sink to your
SIEM if you want a persistent feed.
