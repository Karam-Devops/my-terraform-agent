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


def translate(
    resource: DiscoveredResource,
    *,
    compliance_profile: str = "none",
) -> Translation:
    """Translate GCP Secret Manager → AWS Secrets Manager.

    Compliance profile defaults:
      - kms_encryption: forced True under HIPAA/PCI (overrides default AWS-managed
        KMS with a customer-managed CMK alias; module creates the CMK)
      - automatic_rotation: forced True under HIPAA/PCI (90-day rotation)
    """
    from migrator.translate.compliance_profiles import get_defaults
    _profile_defaults = get_defaults(compliance_profile, "secrets")

    args = resource.arguments or {}
    notes: List[str] = []

    raw_secrets = args.get("secrets") or args.get("secret_configs") or []
    if not isinstance(raw_secrets, list):
        raw_secrets = []

    csv_driven = "bucket_name" in args and "file_name" in args

    # Decide the KMS key alias up-front based on profile.
    # HIPAA/PCI demand a CUSTOMER-MANAGED CMK (not the default AWS-managed key).
    use_customer_kms = bool(_profile_defaults.get("kms_encryption"))
    kms_key_alias = "alias/migrator-secrets-cmk" if use_customer_kms else "alias/aws/secretsmanager"

    secrets = []
    for src in raw_secrets:
        if isinstance(src, dict):
            secrets.append({
                "name":  str(src.get("name", "TODO-secret-name")),
                "kms_key_alias": kms_key_alias,
            })
        elif isinstance(src, str):
            secrets.append({"name": src, "kms_key_alias": kms_key_alias})

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

    # Compliance-profile-driven attrs.
    if use_customer_kms:
        aws_inputs_hcl += "\n  create_customer_kms_key = true   # compliance profile\n"
    rotation_days = _profile_defaults.get("rotation_period_days")
    if _profile_defaults.get("automatic_rotation") and rotation_days:
        aws_inputs_hcl += f"  rotation_period_days    = {rotation_days}     # compliance profile\n"
        notes.append(
            f"compliance profile '{compliance_profile.upper()}' applied — "
            f"customer-managed KMS + {rotation_days}-day automatic rotation enabled. "
            "Operator must provide a Lambda function ARN for the rotation logic "
            "(passes the secret_id env var; emits the rotated value back to Secrets Manager)."
        )
    elif use_customer_kms:
        notes.append(
            f"compliance profile '{compliance_profile.upper()}' applied — "
            "customer-managed KMS key created for envelope encryption."
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

# -----------------------------------------------------------------
# Compliance-profile-driven: customer-managed KMS CMK
# Only deployed when var.create_customer_kms_key = true (set by HIPAA/PCI
# profiles). Replaces the default AWS-managed KMS with a CMK we own.
# -----------------------------------------------------------------
resource "aws_kms_key" "secrets_cmk" {
  count = var.create_customer_kms_key ? 1 : 0

  description             = "${var.name_prefix} secrets manager CMK (HIPAA/PCI envelope encryption)"
  deletion_window_in_days = 30
  enable_key_rotation     = true
  tags                    = var.tags
}

resource "aws_kms_alias" "secrets_cmk" {
  count = var.create_customer_kms_key ? 1 : 0

  name          = "alias/${var.name_prefix}-secrets-cmk"
  target_key_id = aws_kms_key.secrets_cmk[0].key_id
}

resource "aws_secretsmanager_secret" "this" {
  for_each = var.secrets

  name        = each.value.name
  description = "Migrated from GCP Secret Manager"

  # Use the customer-managed CMK when available; otherwise the alias
  # specified per-secret (default: AWS-managed key).
  kms_key_id = var.create_customer_kms_key ? aws_kms_alias.secrets_cmk[0].arn : each.value.kms_key_alias

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

# -----------------------------------------------------------------
# Compliance-profile-driven: automatic rotation policy
# Only deployed when var.rotation_period_days > 0 (set by HIPAA/PCI).
# Operator must supply var.rotation_lambda_arn pointing at their own
# rotation Lambda — Secrets Manager invokes it on the schedule.
# -----------------------------------------------------------------
resource "aws_secretsmanager_secret_rotation" "this" {
  for_each = var.rotation_period_days > 0 && var.rotation_lambda_arn != "" ? var.secrets : {}

  secret_id           = aws_secretsmanager_secret.this[each.key].id
  rotation_lambda_arn = var.rotation_lambda_arn

  rotation_rules {
    automatically_after_days = var.rotation_period_days
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

variable "create_customer_kms_key" {
  type        = bool
  description = "When true, create a customer-managed CMK and use it for envelope encryption of every secret. Required under HIPAA/PCI."
  default     = false
}

variable "rotation_period_days" {
  type        = number
  description = "Days between automatic rotations. 0 disables rotation. HIPAA/PCI: 90."
  default     = 0
}

variable "rotation_lambda_arn" {
  type        = string
  description = "ARN of the rotation Lambda. Only used when rotation_period_days > 0."
  default     = ""
}

variable "name_prefix" {
  type    = string
  default = "migrator"
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
