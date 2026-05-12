"""GCP google_bigquery_dataset → AWS Athena (workgroup + S3) scaffold.

BigQuery has no single-resource AWS analog. Two common targets:

  * **Athena + S3**: serverless SQL over data lake. Best fit when the
    source workload is interactive analytics or ad-hoc queries on
    immutable / append-only data. Cheap to operate.
  * **Redshift Serverless**: column-oriented data warehouse, similar
    query model. Better fit when source has high concurrency / BI
    tooling / complex joins / materialized views.

This translator emits a SCAFFOLD that points at Athena by default
(simpler/cheaper for most healthcare analytics workloads) AND emits
commented-out Redshift Serverless guidance for operator review.

The translator is intentionally SCAFFOLD-ONLY — it doesn't try to
auto-migrate the data. BigQuery export to GCS → S3 sync is part of the
data-migration helpers workstream, not the IaC translation.
"""

from __future__ import annotations

from typing import List

from migrator.results import DiscoveredResource

from .base import DEFAULT_VERSIONS_TF, AWSModuleSpec, Translation


SERVICE_NAME = "athena-workgroup"


def translate(
    resource: DiscoveredResource,
    *,
    compliance_profile: str = "none",
) -> Translation:
    """Translate BigQuery dataset → Athena workgroup + S3 backing.

    Compliance defaults today are minimal — Athena's query-result
    encryption (CSE-KMS / SSE-KMS) lands as a follow-up. The scaffold
    emits the workgroup with operator-facing TODO comments.
    """
    args = resource.arguments or {}
    notes: List[str] = []

    # Datasets can be nested under several source shapes.
    raw_datasets = (
        args.get("dataset_configs")
        or args.get("datasets")
        or args.get("bigquery_config")
        or []
    )
    if isinstance(raw_datasets, dict):
        # dict-of-dicts → list-of-dicts (each item gets name from its key)
        normalized = []
        for k, v in raw_datasets.items():
            if isinstance(v, dict):
                item = dict(v)
                item.setdefault("dataset_id", k)
                normalized.append(item)
        raw_datasets = normalized
    elif not isinstance(raw_datasets, list):
        raw_datasets = []

    datasets = []
    for src in raw_datasets:
        if not isinstance(src, dict):
            continue
        dataset_id = str(
            src.get("dataset_id") or src.get("name") or "TODO-dataset-id"
        )
        dataset_name = str(src.get("dataset_name") or dataset_id)
        description = str(
            src.get("description")
            or f"Migrated from GCP BigQuery dataset {dataset_id}"
        )
        datasets.append({
            "dataset_id":   dataset_id,
            "dataset_name": dataset_name,
            "description":  description,
        })

    if not datasets:
        notes.append("No BigQuery dataset configs detected in source; "
                     "emitted single Athena workgroup placeholder.")
        datasets = [{
            "dataset_id":   "TODO-dataset",
            "dataset_name": "TODO-dataset",
            "description":  "Migrated from GCP BigQuery dataset",
        }]
    else:
        notes.append(
            f"Emitted {len(datasets)} Athena workgroup entr"
            f"{'y' if len(datasets)==1 else 'ies'} (one per BigQuery dataset)."
        )

    notes.append(
        "Athena is the default target for serverless SQL over S3-backed "
        "data. Choose Redshift Serverless instead if the workload needs "
        "high concurrency / BI tooling / complex joins — see commented "
        "alternative in module main.tf."
    )
    notes.append(
        "Data migration is a SEPARATE workstream: BigQuery EXPORT DATA → "
        "GCS bucket → S3 sync → Athena queries the S3 data. The IaC "
        "scaffold here creates the AWS-side targets; helper scripts in "
        "migration_helpers/ cover the data move."
    )
    notes.append(
        "BigQuery views and stored procedures don't translate 1:1; "
        "operator rewrites them as Athena CREATE VIEW statements or "
        "materialized views in Redshift."
    )

    aws_inputs_hcl = (
        "  # Translated from GCP google_bigquery_dataset.\n"
        "  # SCAFFOLD: Athena (default) + commented Redshift alternative.\n"
        f"  datasets = {_render_datasets(datasets)}\n"
        "\n"
        "  # TODO: wire to data-lake S3 bucket (operator supplies)\n"
        '  query_results_bucket = "TODO-athena-query-results-bucket"\n'
    )

    return Translation(
        service_name=SERVICE_NAME,
        aws_inputs_hcl=aws_inputs_hcl,
        notes=notes,
    )


def _render_datasets(datasets: list) -> str:
    if not datasets:
        return "{}"
    lines = ["{"]
    for d in datasets:
        key = (
            d["dataset_id"]
            .replace("-", "_")
            .replace(".", "_")
        )
        lines.append(f'    "{key}" = {{')
        lines.append(f'      dataset_id   = "{d["dataset_id"]}"')
        lines.append(f'      dataset_name = "{d["dataset_name"]}"')
        lines.append(f'      description  = "{d["description"]}"')
        lines.append("    }")
    lines.append("  }")
    return "\n".join(lines)


def aws_module_spec() -> AWSModuleSpec:
    return AWSModuleSpec(
        service_name=SERVICE_NAME,
        main_tf=_MAIN_TF,
        variables_tf=_VARIABLES_TF,
        outputs_tf=_OUTPUTS_TF,
        versions_tf=DEFAULT_VERSIONS_TF,
        readme_md=_README,
    )


_MAIN_TF = '''# AWS Athena Workgroup module — emitted by Cloud Lifecycle Intelligence Migrator.
# Translates GCP BigQuery dataset → Athena (default) or Redshift Serverless (commented).

# ---- Athena workgroups (one per BigQuery dataset) ----
resource "aws_athena_workgroup" "this" {
  for_each = var.datasets

  name        = each.value.dataset_name
  description = each.value.description

  configuration {
    enforce_workgroup_configuration    = true
    publish_cloudwatch_metrics_enabled = true

    result_configuration {
      output_location = "s3://${var.query_results_bucket}/athena-results/${each.value.dataset_name}/"

      encryption_configuration {
        encryption_option = "SSE_S3"
      }
    }
  }

  tags = merge(var.tags, { Name = each.value.dataset_name })
}

# ---- Glue Data Catalog database (Athena's metadata layer) ----
resource "aws_glue_catalog_database" "this" {
  for_each = var.datasets

  name        = each.value.dataset_name
  description = each.value.description
}

# -----------------------------------------------------------------
# Alternative target: Redshift Serverless (UNCOMMENT if your workload
# needs BI tooling / high concurrency / complex joins instead of
# serverless ad-hoc SQL).
# -----------------------------------------------------------------
# resource "aws_redshiftserverless_namespace" "this" {
#   for_each = var.datasets
#
#   namespace_name = each.value.dataset_name
#   db_name        = each.value.dataset_id
#   admin_username = "admin"
#   admin_user_password_secret_arn = var.admin_password_secret_arn   # operator supplies
#
#   tags = var.tags
# }
#
# resource "aws_redshiftserverless_workgroup" "this" {
#   for_each = var.datasets
#
#   namespace_name = aws_redshiftserverless_namespace.this[each.key].namespace_name
#   workgroup_name = each.value.dataset_name
#   base_capacity  = 32   # RPU units; ~32 → ~$22/hr active
#
#   subnet_ids         = var.subnet_ids
#   security_group_ids = [aws_security_group.redshift[0].id]
#   publicly_accessible = false
#
#   tags = var.tags
# }
'''


_VARIABLES_TF = '''variable "datasets" {
  type        = map(any)
  description = <<EOT
Map of dataset key → spec. Required attrs:
  dataset_id   = string
  dataset_name = string
  description  = string
EOT
  default     = {}
}

variable "query_results_bucket" {
  type        = string
  description = "S3 bucket for Athena query results (operator-supplied)."
}

variable "tags" {
  type    = map(string)
  default = {}
}
'''


_OUTPUTS_TF = '''output "workgroup_names" {
  value       = { for k, w in aws_athena_workgroup.this : k => w.name }
  description = "Map of dataset key → Athena workgroup name."
}

output "catalog_database_names" {
  value       = { for k, db in aws_glue_catalog_database.this : k => db.name }
  description = "Map of dataset key → Glue catalog database name."
}
'''


_README = '''# AWS Athena Workgroup module

Translates GCP `google_bigquery_dataset` → Athena workgroup + Glue
Catalog database. Default target is Athena because it fits the typical
BigQuery use case (serverless SQL over data lake) better than Redshift.

## When to switch to Redshift Serverless

Uncomment the Redshift Serverless block in main.tf when:
- You need BI tooling integration (Tableau, Looker, etc.)
- Concurrency exceeds Athena's per-account limit
- Workload has complex multi-table joins or materialized views
- Query latency requirements are sub-second (Athena cold-start is ~5s)

For greenfield analytics workloads, start with Athena. Migrate to
Redshift Serverless if you outgrow it.

## Data migration

This module creates the AWS-side targets. The data migration is a
SEPARATE workstream:

1. **Export from BigQuery**: `bq extract --destination_format=PARQUET ...`
   to a GCS bucket
2. **Sync GCS → S3**: use the `01-gcs-to-s3-sync.sh` helper script
3. **Register S3 prefixes with Glue**: create Glue tables pointing
   at the S3 prefix per dataset
4. **Query via Athena**: `SELECT ... FROM dataset.table`

## Compliance defaults

| Profile | Encryption | Audit logging |
|---|---|---|
| none  | SSE-S3 (AES-256) | CloudWatch metrics enabled |
| hipaa | SSE-KMS (CMK)    | + CloudTrail data events on the results bucket |
| pci   | SSE-KMS (CMK)    | + CloudTrail data events on the results bucket |

(SSE-KMS upgrade is a future translator iteration.)
'''
