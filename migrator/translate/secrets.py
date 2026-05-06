"""GCP google_secret_manager_secret → AWS aws_secretsmanager_secret.

Source pattern (varies — common shapes):
  - Customer's CSV-driven module: bucket_name, file_name
  - Direct list: secrets = [{name, replication}, ...]

We handle both: CSV-driven becomes a TODO with operator note about
importing values via Lambda/script post-deploy; direct list becomes
1:1 secret resources.
"""

from __future__ import annotations

from typing import List

from migrator.results import DiscoveredResource

from .base import DEFAULT_VERSIONS_TF, AWSModuleSpec, Translation


SERVICE_NAME = "secretsmanager-secret"


def translate(resource: DiscoveredResource) -> Translation:
    args = resource.arguments or {}
    notes: List[str] = []

    raw_secrets = args.get("secrets") or args.get("secret_configs") or []
    if not isinstance(raw_secrets, list):
        raw_secrets = []

    csv_driven = "bucket_name" in args and "file_name" in args

    secrets = []
    for src in raw_secrets:
        if isinstance(src, dict):
            secrets.append({
                "name":  str(src.get("name", "TODO-secret-name")),
                "kms_key_alias": "alias/aws/secretsmanager",
            })
        elif isinstance(src, str):
            secrets.append({"name": src, "kms_key_alias": "alias/aws/secretsmanager"})

    if csv_driven:
        notes.append(
            f"Source uses a CSV-driven secret import pattern "
            f"(bucket_name={args.get('bucket_name')!r}, file_name={args.get('file_name')!r}). "
            f"Translation emits the AWS Secrets Manager module skeleton; "
            f"the CSV → secret-value import needs a one-time Lambda or a "
            f"`migration_helpers/03-secrets-migrate.sh` run."
        )
    if not secrets and not csv_driven:
        notes.append("No secrets list found in source; emitted empty map.")
    if secrets:
        notes.append(f"Emitted {len(secrets)} Secrets Manager secret entries.")

    aws_inputs_hcl = (
        "  # Translated from GCP google_secret_manager_secret.\n"
        f"  secrets = {_render_secrets(secrets)}\n"
    )

    if csv_driven:
        aws_inputs_hcl += (
            "\n"
            f'  # CSV-driven import (source GCP pattern):\n'
            f'  #   bucket_name = "{args.get("bucket_name", "TODO")}"\n'
            f'  #   file_name   = "{args.get("file_name", "TODO")}"\n'
            "  # Use migration_helpers/03-secrets-migrate.sh to import values from CSV → AWS Secrets Manager.\n"
        )

    return Translation(
        service_name=SERVICE_NAME,
        aws_inputs_hcl=aws_inputs_hcl,
        notes=notes,
    )


def _render_secrets(secrets: list) -> str:
    if not secrets:
        return "{}"
    lines = ["{"]
    for s in secrets:
        key = s["name"].replace("-", "_").replace(".", "_").replace("/", "_")
        lines.append(f'    "{key}" = {{')
        lines.append(f'      name          = "{s["name"]}"')
        lines.append(f'      kms_key_alias = "{s["kms_key_alias"]}"')
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


_MAIN_TF = '''# AWS Secrets Manager module — emitted by Cloud Lifecycle Intelligence Migrator.
# Translates GCP google_secret_manager_secret. Each entry creates one
# aws_secretsmanager_secret with a placeholder version (operator
# imports real values via migration_helpers/03-secrets-migrate.sh).

resource "aws_secretsmanager_secret" "this" {
  for_each = var.secrets

  name        = each.value.name
  description = "Migrated from GCP Secret Manager"
  kms_key_id  = each.value.kms_key_alias

  tags = merge(
    var.tags,
    { Name = each.value.name },
  )
}

# Placeholder version so plan succeeds on first run; replace with
# real value via migration_helpers/03-secrets-migrate.sh after deploy.
resource "aws_secretsmanager_secret_version" "placeholder" {
  for_each      = var.create_placeholders ? var.secrets : {}
  secret_id     = aws_secretsmanager_secret.this[each.key].id
  secret_string = jsonencode({ placeholder = "REPLACE-VIA-MIGRATION-HELPER" })

  lifecycle {
    ignore_changes = [secret_string]
  }
}
'''


_VARIABLES_TF = '''variable "secrets" {
  type = map(object({
    name          = string
    kms_key_alias = string  # e.g. "alias/aws/secretsmanager" (default) or custom KMS alias
  }))
  description = "Map of secret key -> spec. Each becomes one aws_secretsmanager_secret."
  default     = {}
}

variable "create_placeholders" {
  type        = bool
  description = "Whether to create initial placeholder secret versions (true for fresh deploy)."
  default     = true
}

variable "tags" {
  type    = map(string)
  default = {}
}
'''


_OUTPUTS_TF = '''output "secret_arns" {
  value     = { for k, s in aws_secretsmanager_secret.this : k => s.arn }
  description = "Map of secret key -> ARN."
  sensitive = true
}

output "secret_names" {
  value = { for k, s in aws_secretsmanager_secret.this : k => s.name }
  description = "Map of secret key -> name."
}
'''


_README = '''# AWS Secrets Manager module

Translates GCP `google_secret_manager_secret`. Each entry → one Secrets
Manager secret with a placeholder version so `terragrunt plan` succeeds
on first deploy.

## Post-deploy: import real values

GCP Secret Manager values are NOT migrated by this module. Run
`migration_helpers/03-secrets-migrate.sh` after `terragrunt apply` to
copy values from GCP Secret Manager → AWS Secrets Manager. The script
authenticates against both clouds, reads each secret's latest version
from GCP, and writes it to AWS.

## CSV-driven import case

If the source GCP module read secrets from a CSV in GCS (`bucket_name` +
`file_name` inputs), the equivalent AWS pattern is:
1. Upload the CSV to S3
2. Run a Lambda that reads the CSV and writes each row as a Secrets
   Manager version

`migration_helpers/03-secrets-migrate.sh` handles the per-row write.
The CSV upload and Lambda wiring is a separate workstream.
'''
