"""Generate data-migration helper scripts based on resource inventory.

Templates render bash scripts that move data from GCP to AWS for
specific service families:
  * GCS → S3 (gsutil rsync + aws s3 sync)
  * Cloud SQL → RDS (DMS-based)
  * Secret Manager → AWS Secrets Manager (script reads + writes)
  * Memorystore → ElastiCache (manual snapshot recommendation)

Scripts use placeholder values that the operator fills in (project IDs,
bucket names, ARNs). Goal: ready-to-edit starters, not turnkey.
"""

from __future__ import annotations

import os
from typing import List

from migrator.results import ConfidenceFinding


def emit_helper_scripts(
    *,
    output_dir: str,
    target_cloud: str,
    confidence: List[ConfidenceFinding],
    aws_region: str = "us-east-1",
) -> List[str]:
    """Emit one helper script per service family present in the inventory.

    Args:
        aws_region: target AWS region to bake into the helper scripts.
            HIPAA-compliant regions in the customer's actual deployment
            target. Defaults to us-east-1 to match the engine's default.

    Returns absolute paths of generated files.
    """
    if target_cloud.lower() != "aws":
        return []

    helpers_dir = os.path.join(output_dir, "migration_helpers")
    os.makedirs(helpers_dir, exist_ok=True)

    # Substitute region placeholder in every template so the operator
    # gets aws-cli commands that point at their actual region. Previously
    # the templates had ca-central-1 hardcoded — wrong for the HIPAA
    # us-east-1 default. Variable substitution: __AWS_REGION__ →
    # aws_region argument.
    def _substituted(template: str) -> str:
        return template.replace("__AWS_REGION__", aws_region)

    # Detect which service families need helpers based on tf_types present.
    types_present = {c.tf_type for c in confidence}

    written: List[str] = []

    if "google_storage_bucket" in types_present:
        path = _write(helpers_dir, "01-gcs-to-s3-sync.sh", _substituted(_GCS_TO_S3_TEMPLATE))
        written.append(path)

    if any(t in types_present for t in (
        "google_sql_database_instance", "google_sql_database",
    )):
        path = _write(helpers_dir, "02-cloudsql-to-rds-dms.sh",
                      _substituted(_CLOUDSQL_TO_RDS_TEMPLATE))
        written.append(path)

    if "google_secret_manager_secret" in types_present:
        path = _write(helpers_dir, "03-secrets-migrate.sh",
                      _substituted(_SECRETS_MIGRATE_TEMPLATE))
        written.append(path)

    if "google_redis_instance" in types_present:
        path = _write(helpers_dir, "04-memorystore-to-elasticache.sh",
                      _substituted(_MEMORYSTORE_TO_ELASTICACHE_TEMPLATE))
        written.append(path)

    if "google_artifact_registry_repository" in types_present:
        path = _write(helpers_dir, "05-artifact-registry-to-ecr.sh",
                      _substituted(_ARTIFACT_TO_ECR_TEMPLATE))
        written.append(path)

    if "google_pubsub_topic" in types_present:
        path = _write(helpers_dir, "06-pubsub-to-sns-sqs-replay.md",
                      _substituted(_PUBSUB_TO_SNS_NOTES))
        written.append(path)

    # Always emit the high-level checklist
    path = _write(helpers_dir, "00-MIGRATION_CHECKLIST.md", _substituted(_CHECKLIST_TEMPLATE))
    written.append(path)

    return written


def _write(dirpath: str, filename: str, content: str) -> str:
    full = os.path.join(dirpath, filename)
    with open(full, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(content)
    # bash scripts get +x where the OS supports it (no-op on Windows)
    if filename.endswith(".sh"):
        try:
            os.chmod(full, 0o755)
        except OSError:
            pass
    return full


# -----------------------------------------------------------------
# Templates
# -----------------------------------------------------------------

_GCS_TO_S3_TEMPLATE = """#!/usr/bin/env bash
# 01-gcs-to-s3-sync.sh
# Sync GCS buckets to S3 buckets. Run AFTER S3 buckets are created via Terraform.
#
# Prereqs:
#   - gsutil installed + authenticated against the source GCP project
#   - aws CLI installed + authenticated against the target AWS account
#   - One-line BUCKETS list below: each line is "gcs_bucket_name s3_bucket_name"
#
# Strategy: gsutil -> stdout, aws s3 cp -> stdin via streaming pipe.
# For very large buckets, use `gsutil rsync` to a local staging dir first.

set -euo pipefail

# EDIT BELOW: each line is "<gcs-bucket> <s3-bucket>".
read -r -d '' BUCKETS <<'EOF' || true
# example-bucket-1   target-s3-bucket-1
# example-bucket-2   target-s3-bucket-2
EOF

while read -r gcs_bucket s3_bucket; do
  [[ "${gcs_bucket}" =~ ^[[:space:]]*# ]] && continue
  [[ -z "${gcs_bucket:-}" ]] && continue

  echo ">>> Syncing gs://${gcs_bucket}/ -> s3://${s3_bucket}/"

  # Approach 1 (small/medium buckets): gsutil cp with stream-to-S3.
  # Approach 2 (large buckets): use AWS DataSync GCS source connector
  #   or AWS Snowball if egress costs are significant.

  gsutil -m cp -r "gs://${gcs_bucket}/*" - 2>/dev/null \\
    | aws s3 cp - "s3://${s3_bucket}/" --recursive \\
    || echo "WARN: sync failed for ${gcs_bucket}; investigate before retrying."
done <<< "${BUCKETS}"

echo ""
echo "Done. Verify object counts with:"
echo "  gsutil ls -l gs://<bucket> | wc -l"
echo "  aws s3 ls s3://<bucket> --recursive | wc -l"
"""


_CLOUDSQL_TO_RDS_TEMPLATE = """#!/usr/bin/env bash
# 02-cloudsql-to-rds-dms.sh
# Migrate Cloud SQL Postgres / MySQL to AWS RDS via AWS DMS.
#
# Prereqs:
#   - RDS target instance exists and is reachable from the DMS replication instance
#   - DMS replication instance exists in the target VPC
#   - Source endpoint: a publicly-routable Cloud SQL instance OR Cloud SQL via PSA
#     reachable from AWS (HA VPN or Direct Connect)
#   - Source DB user has REPLICATION grants
#
# This script is a guided runbook, not a turnkey runner — DMS task creation
# benefits from per-instance review of full-load vs ongoing-replication settings.

set -euo pipefail

# EDIT BELOW
SOURCE_HOST="<cloudsql-public-or-psa-ip>"
SOURCE_PORT="5432"
SOURCE_DB="<dbname>"
SOURCE_USER="<replication-user>"
SOURCE_PASSWORD_SECRET_ARN="<secrets-manager-arn-for-source-password>"

TARGET_RDS_ARN="<arn:aws:rds:region:acct:db:dbname>"
DMS_INSTANCE_ARN="<arn:aws:dms:region:acct:rep:dms-instance>"
DMS_ROLE_ARN="<arn:aws:iam::acct:role/dms-vpc-role>"

REGION="__AWS_REGION__"

echo "Step 1: create source endpoint (Cloud SQL)"
aws dms create-endpoint --region "${REGION}" \\
  --endpoint-identifier cloudsql-source \\
  --endpoint-type source --engine-name postgres \\
  --server-name "${SOURCE_HOST}" --port "${SOURCE_PORT}" \\
  --database-name "${SOURCE_DB}" --username "${SOURCE_USER}" \\
  --password "$(aws secretsmanager get-secret-value --secret-id "${SOURCE_PASSWORD_SECRET_ARN}" --query SecretString --output text)"

echo "Step 2: create target endpoint (RDS)"
aws dms create-endpoint --region "${REGION}" \\
  --endpoint-identifier rds-target \\
  --endpoint-type target --engine-name aurora-postgresql \\
  --server-name "$(aws rds describe-db-instances --db-instance-identifier <dbname> --query 'DBInstances[0].Endpoint.Address' --output text)" \\
  --port 5432 --database-name "${SOURCE_DB}" --username "<rds-admin>" \\
  --password "<password>"

echo "Step 3: create replication task (full-load + CDC)"
aws dms create-replication-task --region "${REGION}" \\
  --replication-task-identifier cloudsql-to-rds \\
  --source-endpoint-arn <source-arn-from-step-1> \\
  --target-endpoint-arn <target-arn-from-step-2> \\
  --replication-instance-arn "${DMS_INSTANCE_ARN}" \\
  --migration-type full-load-and-cdc \\
  --table-mappings file://table-mappings.json \\
  --replication-task-settings file://task-settings.json

echo "Step 4: start replication"
aws dms start-replication-task --region "${REGION}" \\
  --replication-task-arn <task-arn-from-step-3> \\
  --start-replication-task-type start-replication

echo ""
echo "Monitor via:"
echo "  aws dms describe-replication-tasks --region ${REGION}"
echo "  aws dms describe-table-statistics --region ${REGION} --replication-task-arn <task-arn>"
"""


_SECRETS_MIGRATE_TEMPLATE = """#!/usr/bin/env bash
# 03-secrets-migrate.sh
# Migrate GCP Secret Manager secrets to AWS Secrets Manager.
#
# Prereqs:
#   - gcloud authenticated against source GCP project
#   - aws CLI authenticated against target AWS account
#   - jq installed
#
# Reads each named secret's latest version from GCP and writes it to
# AWS Secrets Manager with the same name.

set -euo pipefail

# EDIT BELOW
SOURCE_PROJECT="<gcp-project-id>"
TARGET_REGION="__AWS_REGION__"
SECRETS=(
  # one secret name per line
  # "example-secret-1"
  # "example-secret-2"
)

for secret in "${SECRETS[@]}"; do
  echo ">>> Migrating ${secret}"

  # Read latest version from GCP.
  payload=$(gcloud secrets versions access latest \\
    --secret="${secret}" --project="${SOURCE_PROJECT}" 2>/dev/null) || {
    echo "  WARN: failed to read source secret ${secret}; skipping."
    continue
  }

  # Create or update target secret.
  if aws secretsmanager describe-secret --secret-id "${secret}" --region "${TARGET_REGION}" >/dev/null 2>&1; then
    aws secretsmanager put-secret-value \\
      --secret-id "${secret}" --region "${TARGET_REGION}" \\
      --secret-string "${payload}" >/dev/null
    echo "  Updated existing AWS secret."
  else
    aws secretsmanager create-secret \\
      --name "${secret}" --region "${TARGET_REGION}" \\
      --secret-string "${payload}" >/dev/null
    echo "  Created new AWS secret."
  fi
done

echo ""
echo "Done. Verify with:"
echo "  aws secretsmanager list-secrets --region ${TARGET_REGION}"
"""


_MEMORYSTORE_TO_ELASTICACHE_TEMPLATE = """#!/usr/bin/env bash
# 04-memorystore-to-elasticache.sh
# Migrate GCP Memorystore Redis -> AWS ElastiCache Redis.
#
# Memorystore exports RDB snapshots to GCS; ElastiCache imports from S3.
# Net: snapshot -> GCS -> sync to S3 -> ElastiCache restore.

set -euo pipefail

# EDIT BELOW
SOURCE_INSTANCE="<memorystore-instance-id>"
SOURCE_REGION="northamerica-northeast1"
SOURCE_PROJECT="<gcp-project-id>"
GCS_EXPORT_BUCKET="<your-export-bucket>"

TARGET_S3_BUCKET="<target-s3-bucket>"
TARGET_REGION="__AWS_REGION__"
TARGET_ELASTICACHE_NAME="<target-elasticache-name>"

echo "Step 1: export Memorystore snapshot to GCS"
gcloud redis instances export "gs://${GCS_EXPORT_BUCKET}/${SOURCE_INSTANCE}.rdb" \\
  --instance="${SOURCE_INSTANCE}" --region="${SOURCE_REGION}" --project="${SOURCE_PROJECT}"

echo "Step 2: copy snapshot to S3"
gsutil cp "gs://${GCS_EXPORT_BUCKET}/${SOURCE_INSTANCE}.rdb" - \\
  | aws s3 cp - "s3://${TARGET_S3_BUCKET}/${SOURCE_INSTANCE}.rdb" --region "${TARGET_REGION}"

echo "Step 3: restore into ElastiCache (must already exist via Terraform)"
aws elasticache create-replication-group \\
  --region "${TARGET_REGION}" \\
  --replication-group-id "${TARGET_ELASTICACHE_NAME}" \\
  --snapshot-arns "arn:aws:s3:::${TARGET_S3_BUCKET}/${SOURCE_INSTANCE}.rdb"

echo "Done. Note: this overwrites any existing data in the target."
"""


_ARTIFACT_TO_ECR_TEMPLATE = """#!/usr/bin/env bash
# 05-artifact-registry-to-ecr.sh
# Mirror images from GCP Artifact Registry to AWS ECR.

set -euo pipefail

# EDIT BELOW
SOURCE_PROJECT="<gcp-project-id>"
SOURCE_REGION="northamerica-northeast1"
SOURCE_REGISTRY="<artifact-registry-repo-name>"

TARGET_ACCOUNT="<aws-account-id>"
TARGET_REGION="__AWS_REGION__"
TARGET_ECR_REPO="<ecr-repo-name>"

# Authenticate Docker to both registries.
gcloud auth configure-docker "${SOURCE_REGION}-docker.pkg.dev" --quiet
aws ecr get-login-password --region "${TARGET_REGION}" \\
  | docker login --username AWS --password-stdin \\
    "${TARGET_ACCOUNT}.dkr.ecr.${TARGET_REGION}.amazonaws.com"

# List source images and copy each tag.
gcloud artifacts docker images list \\
  "${SOURCE_REGION}-docker.pkg.dev/${SOURCE_PROJECT}/${SOURCE_REGISTRY}" \\
  --format='value(IMAGE)' | while read -r image; do
  for tag in $(gcloud artifacts docker tags list "${image}" --format='value(TAG)'); do
    src="${image}:${tag}"
    dst="${TARGET_ACCOUNT}.dkr.ecr.${TARGET_REGION}.amazonaws.com/${TARGET_ECR_REPO}:${tag}"
    echo ">>> ${src}  =>  ${dst}"
    docker pull "${src}"
    docker tag  "${src}" "${dst}"
    docker push "${dst}"
  done
done
"""


_PUBSUB_TO_SNS_NOTES = """# Pub/Sub → SNS+SQS migration notes

GCP Pub/Sub combines a topic and its subscriptions into one resource family.
On AWS, the equivalent is a fan-out via SNS topics (publish side) and SQS
queues (subscriber side). There is **no automated data-migration script**:

1. **Topology change.** Each GCP subscription becomes either:
   - An SQS queue subscribed to the SNS topic (most common).
   - An HTTP/HTTPS endpoint subscription (for push subscriptions).
   - A Lambda subscription (for serverless workloads).

2. **In-flight messages.** During cutover, drain the GCP subscription queue
   to zero **before** switching publishers to SNS. Otherwise messages
   in-flight on the GCP side are lost.

3. **Ordering.** GCP Pub/Sub supports message ordering via ordering keys.
   AWS SQS supports FIFO with message group IDs. Translation is straightforward
   but the AWS FIFO queue type is a different SKU — review per-queue.

4. **Dead-letter topics.** GCP DLQ topics translate to SQS DLQ queues. Map
   the `dead_letter_policy.dead_letter_topic` field to a separate `aws_sqs_queue`
   used in the main queue's `redrive_policy`.

5. **Filtering.** GCP subscription filters use a custom syntax. AWS SNS filters
   use JSON-encoded match conditions. Most filter expressions translate but
   complex `hasPrefix` / `hasSubstring` patterns need rewriting.

## Cutover procedure

1. Provision SNS topic + SQS queues (this is what `aws_sns_topic` + `aws_sqs_queue`
   in your generated Terraform handle).
2. Subscribe each consumer to the SNS topic via SQS.
3. Update producers to publish to **both** GCP Pub/Sub and AWS SNS in parallel.
4. Drain GCP subscriptions to zero. Verify with `gcloud pubsub subscriptions describe`.
5. Switch producers to AWS SNS only.
6. Decommission GCP topics.
"""


_CHECKLIST_TEMPLATE = """# Migration helpers checklist

Run these scripts in order **after** the AWS Terraform / Terragrunt has been
applied (so target resources exist) and **before** cutting over production
traffic.

| Order | Script | Purpose |
|---|---|---|
| 0 | `00-MIGRATION_CHECKLIST.md` | This file |
| 1 | `01-gcs-to-s3-sync.sh` | Sync GCS bucket contents to S3 |
| 2 | `02-cloudsql-to-rds-dms.sh` | Cloud SQL → RDS via AWS DMS |
| 3 | `03-secrets-migrate.sh` | Secret Manager → AWS Secrets Manager |
| 4 | `04-memorystore-to-elasticache.sh` | Memorystore → ElastiCache |
| 5 | `05-artifact-registry-to-ecr.sh` | Mirror Docker images to ECR |
| 6 | `06-pubsub-to-sns-sqs-replay.md` | Pub/Sub → SNS+SQS notes |

## Cut-over recipe (high level)

1. **Pre-flight** — verify all generated Terraform passes `terraform plan`
   in the AWS sandbox account.
2. **Apply infrastructure** — `terraform apply` (or `terragrunt run-all apply`)
   in dependency order. Cross-reference `MIGRATION_GUIDE.md` for the
   ordered sequence.
3. **Run helpers in order** — each script above. Many are idempotent and
   safe to re-run; verify with the script's `Done.` output before proceeding.
4. **Validate** — application-level smoke tests against the AWS endpoints.
5. **Traffic switch** — Route 53 weighted routing, gradually shift from
   GCP origins to AWS origins. Watch error rates per service.
6. **Decommission** — only after 7+ days of stable AWS-only traffic, begin
   tearing down GCP resources. Keep state files and snapshots for 90+ days.

## Rollback

If anything fails: pause the helper run, do not run `terraform destroy`
on the AWS side, restore Route 53 weights to favor GCP, and investigate.
The failure point + last successful step are recorded in each script's
stdout — preserve those logs.
"""
