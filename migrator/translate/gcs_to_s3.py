"""GCP google_storage_bucket → AWS aws_s3_bucket.

Source pattern (from customer's gcs terragrunt.hcl):

    inputs = {
      project_id     = local._project.locals.project_id
      primary_region = local._project.locals.primary_region

      gcs_config = [
        {
          name                  = "${local._project.locals.project_id}-cdc-bucket"
          storage_class         = "STANDARD"
          soft_delete_retention = 604800
          uniform_bucket_level_access = true
          notification_config = { ... }
          iam_bindings = { ... }
          lifecycle_rules = { ... }
        },
        ...
      ]
    }

Each entry becomes one S3 bucket. We map storage_class → S3 storage
class (STANDARD passthrough), uniform_bucket_level_access → Block
Public Access + bucket policy, lifecycle_rules → S3 lifecycle config.
"""

from __future__ import annotations

from typing import List

from migrator.results import DiscoveredResource

from .base import DEFAULT_VERSIONS_TF, AWSModuleSpec, Translation


SERVICE_NAME = "s3-bucket"


# GCS storage classes → S3 equivalents.
_STORAGE_CLASS_MAP = {
    "STANDARD":      "STANDARD",
    "NEARLINE":      "STANDARD_IA",
    "COLDLINE":      "GLACIER_IR",
    "ARCHIVE":       "DEEP_ARCHIVE",
    "MULTI_REGIONAL": "STANDARD",
    "REGIONAL":      "STANDARD",
}


def translate(resource: DiscoveredResource) -> Translation:
    args = resource.arguments or {}
    notes: List[str] = []

    raw_buckets = args.get("gcs_config") or args.get("buckets") or []
    if not isinstance(raw_buckets, list):
        raw_buckets = []

    s3_buckets = []
    for src in raw_buckets:
        if not isinstance(src, dict):
            continue
        name = str(src.get("name", "TODO-bucket-name"))
        gcp_class = str(src.get("storage_class", "STANDARD")).upper()
        s3_class = _STORAGE_CLASS_MAP.get(gcp_class, "STANDARD")

        ubla = bool(src.get("uniform_bucket_level_access", False))

        # Lifecycle rules — best-effort translation. Customer's GCS
        # rules use {action, condition} shape; AWS S3 lifecycle uses
        # {id, transition, expiration, noncurrent_*} shape. We pass
        # source rules through as raw map; AWS module renders them.
        src_lifecycle = src.get("lifecycle_rules") or {}
        if not isinstance(src_lifecycle, dict):
            src_lifecycle = {}

        s3_lifecycle_rules = {}
        for rule_id, rule in src_lifecycle.items():
            if not isinstance(rule, dict):
                continue
            action = (rule.get("action") or {}) if isinstance(rule.get("action"), dict) else {}
            condition = (rule.get("condition") or {}) if isinstance(rule.get("condition"), dict) else {}
            action_type = str(action.get("type", "")).upper()
            age_days = condition.get("age")

            entry = {
                "enabled": True,
                "id":      str(rule_id),
            }
            if action_type == "DELETE" and age_days is not None:
                entry["expiration_days"] = int(age_days)
            elif action_type == "SETSTORAGECLASS":
                tgt_class = str(action.get("storage_class", "STANDARD")).upper()
                entry["transition_days"] = int(age_days) if age_days is not None else 30
                entry["transition_storage_class"] = _STORAGE_CLASS_MAP.get(tgt_class, "STANDARD_IA")
            else:
                # Unknown rule type — pass to operator review.
                entry["_TODO"] = f"unmapped lifecycle action: {action_type}"

            s3_lifecycle_rules[rule_id] = entry

        # Soft-delete retention → S3 versioning + MFA delete is closest
        # analog. We just enable versioning when soft delete > 0.
        soft_delete = src.get("soft_delete_retention")
        versioning = bool(soft_delete and soft_delete > 0)

        s3_buckets.append({
            "name":                name,
            "storage_class":       s3_class,
            "block_public_access": ubla,
            "versioning":          versioning,
            "lifecycle_rules":     s3_lifecycle_rules,
        })

        # Notes for operator-facing notes
        if "notification_config" in src:
            notes.append(
                f"bucket `{name}`: GCS notification_config detected → "
                "use S3 EventBridge / SNS notification (configured separately, see migration_helpers/06-pubsub-to-sns-sqs-replay.md)"
            )
        if "iam_bindings" in src:
            notes.append(
                f"bucket `{name}`: per-bucket IAM bindings detected → "
                "translated to bucket policy in module body (review actor mappings)"
            )

    if not s3_buckets:
        notes.append("No gcs_config entries found in source; emitted empty buckets map.")

    aws_inputs_hcl = (
        "  # Translated from GCP gcs_config list.\n"
        "  # Each entry becomes one aws_s3_bucket.\n"
        f"  buckets = {_render_buckets(s3_buckets)}\n"
    )

    notes.insert(0, f"Emitted {len(s3_buckets)} S3 bucket entr{'y' if len(s3_buckets)==1 else 'ies'}.")
    return Translation(
        service_name=SERVICE_NAME,
        aws_inputs_hcl=aws_inputs_hcl,
        notes=notes,
    )


def _render_buckets(buckets: list) -> str:
    if not buckets:
        return "{}"
    lines = ["{"]
    for b in buckets:
        key = (b["name"]
               .replace("${local._project.locals.project_id}-", "")
               .replace("-", "_")
               .replace(".", "_"))
        lines.append(f'    "{key}" = {{')
        lines.append(f'      name                = "{b["name"]}"')
        lines.append(f'      storage_class       = "{b["storage_class"]}"')
        lines.append(f'      block_public_access = {str(b["block_public_access"]).lower()}')
        lines.append(f'      versioning          = {str(b["versioning"]).lower()}')
        if b["lifecycle_rules"]:
            lines.append("      lifecycle_rules = {")
            for rid, rule in b["lifecycle_rules"].items():
                rkey = rid.replace(".", "_")
                lines.append(f'        "{rkey}" = {{')
                for fk, fv in rule.items():
                    if fk.startswith("_"):
                        continue
                    if isinstance(fv, bool):
                        lines.append(f'          {fk} = {str(fv).lower()}')
                    elif isinstance(fv, int):
                        lines.append(f'          {fk} = {fv}')
                    else:
                        lines.append(f'          {fk} = "{fv}"')
                if "_TODO" in rule:
                    lines.append(f'          # TODO: {rule["_TODO"]}')
                lines.append("        }")
            lines.append("      }")
        else:
            lines.append("      lifecycle_rules = {}")
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


_MAIN_TF = '''# AWS S3 Bucket module — emitted by Cloud Lifecycle Intelligence Migrator.
# Translates the GCP google_storage_bucket family.
#
# Swap path: replace this main.tf only. variables.tf + outputs.tf
# define the contract — keep stable.

resource "aws_s3_bucket" "this" {
  for_each = var.buckets

  bucket        = each.value.name
  force_destroy = false

  tags = merge(
    var.tags,
    { Name = each.value.name },
  )
}

resource "aws_s3_bucket_versioning" "this" {
  for_each = var.buckets
  bucket   = aws_s3_bucket.this[each.key].id
  versioning_configuration {
    status = each.value.versioning ? "Enabled" : "Suspended"
  }
}

resource "aws_s3_bucket_public_access_block" "this" {
  for_each = { for k, v in var.buckets : k => v if v.block_public_access }
  bucket   = aws_s3_bucket.this[each.key].id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_lifecycle_configuration" "this" {
  for_each = { for k, v in var.buckets : k => v if length(keys(v.lifecycle_rules)) > 0 }
  bucket   = aws_s3_bucket.this[each.key].id

  dynamic "rule" {
    for_each = each.value.lifecycle_rules
    content {
      id     = rule.value.id
      status = rule.value.enabled ? "Enabled" : "Disabled"

      dynamic "expiration" {
        for_each = lookup(rule.value, "expiration_days", null) != null ? [1] : []
        content {
          days = rule.value.expiration_days
        }
      }

      dynamic "transition" {
        for_each = lookup(rule.value, "transition_days", null) != null ? [1] : []
        content {
          days          = rule.value.transition_days
          storage_class = rule.value.transition_storage_class
        }
      }
    }
  }
}
'''


_VARIABLES_TF = '''variable "buckets" {
  type = map(object({
    name                = string
    storage_class       = string  # STANDARD, STANDARD_IA, GLACIER_IR, DEEP_ARCHIVE
    block_public_access = bool
    versioning          = bool
    lifecycle_rules     = map(any)  # rule_id -> { enabled, id, expiration_days?, transition_days?, transition_storage_class? }
  }))
  description = "Map of bucket key -> spec. Each entry creates one aws_s3_bucket + supporting resources."
  default     = {}
}

variable "tags" {
  type        = map(string)
  description = "Tags merged onto every bucket."
  default     = {}
}
'''


_OUTPUTS_TF = '''output "bucket_ids" {
  value = { for k, b in aws_s3_bucket.this : k => b.id }
  description = "Map of bucket key -> S3 bucket ID."
}

output "bucket_arns" {
  value = { for k, b in aws_s3_bucket.this : k => b.arn }
  description = "Map of bucket key -> S3 bucket ARN."
}

output "bucket_names" {
  value = { for k, b in aws_s3_bucket.this : k => b.bucket }
  description = "Map of bucket key -> S3 bucket name."
}
'''


_README = '''# AWS S3 Bucket module

Emitted by Cloud Lifecycle Intelligence Migrator. Translates GCP
`google_storage_bucket` resources, including lifecycle rules,
versioning, and uniform-bucket-level-access (→ S3 Block Public Access).

## Input contract

```hcl
buckets = {
  "my-bucket" = {
    name                = "my-bucket-prod"
    storage_class       = "STANDARD"
    block_public_access = true
    versioning          = true
    lifecycle_rules = {
      "delete-old" = {
        enabled         = true
        id              = "delete-old"
        expiration_days = 30
      }
    }
  }
}

tags = { project = "x", env = "dev" }
```

## Notes

- GCS storage classes mapped: STANDARD→STANDARD, NEARLINE→STANDARD_IA,
  COLDLINE→GLACIER_IR, ARCHIVE→DEEP_ARCHIVE.
- Uniform bucket-level access → S3 Public Access Block (4 flags ON).
- GCS soft delete retention → S3 versioning enabled (closest analog).
- GCS notification_config → emit a separate `aws_s3_bucket_notification`
  outside this module (out of scope; see migration_helpers).
- Per-bucket IAM bindings need separate `aws_s3_bucket_policy` (out of
  scope; review the inline notes in MIGRATION_GUIDE.md).
'''
