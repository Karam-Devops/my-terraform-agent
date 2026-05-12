"""GCP google_logging_project_sink → AWS Kinesis Firehose + CloudWatch.

Source pattern (varies — common shapes):

    inputs = {
      sinks = {
        "audit-sink" = {
          name        = "..."
          destination = "pubsub.googleapis.com/projects/.../topics/..."
          filter      = "logName=\"projects/.../logs/audit\""
          unique_writer_identity = true
        }
      }
    }

GCP log routing to Pub/Sub → AWS pattern: CloudWatch Logs subscription
filter → Kinesis Firehose → S3 bucket. Long-term storage = S3, not
BigQuery (closer to Athena+S3 if querying needed).
"""

from __future__ import annotations

from typing import List

from migrator.results import DiscoveredResource

from .base import DEFAULT_VERSIONS_TF, AWSModuleSpec, Translation


SERVICE_NAME = "log-sink-firehose"


def translate(resource: DiscoveredResource) -> Translation:
    args = resource.arguments or {}
    notes: List[str] = []

    # Source key variants observed across customer repos:
    #   * sinks                — vanilla module library, dict-of-dicts
    #   * log_sinks            — alias
    #   * sink_configs         — alias
    #   * log_sink_configs     — DH customer pattern, list-of-dicts where
    #                            each item has log_sink_name / destination_uri
    raw_sinks = (
        args.get("sinks")
        or args.get("log_sinks")
        or args.get("sink_configs")
        or args.get("log_sink_configs")
        or {}
    )
    if isinstance(raw_sinks, list):
        # Each item may use `log_sink_name` (DH) or `name` (vanilla)
        # as its identifier. Normalize to a map keyed by name.
        raw_sinks = {
            s.get("log_sink_name") or s.get("name") or f"sink{i}": s
            for i, s in enumerate(raw_sinks) if isinstance(s, dict)
        }
    if not isinstance(raw_sinks, dict):
        raw_sinks = {}

    sinks = []
    for key, src in raw_sinks.items():
        if not isinstance(src, dict):
            continue
        # `log_sink_name` is DH's per-item identifier; `name` is vanilla.
        name = str(src.get("name") or src.get("log_sink_name") or key)
        # `destination_uri` is DH's destination field; `destination` is vanilla.
        destination = str(src.get("destination") or src.get("destination_uri") or "")
        filter_expr = str(src.get("filter", ""))

        # Detect destination type from the destination URL
        dest_type = "s3"
        if "pubsub.googleapis.com" in destination:
            dest_type = "firehose_to_s3"
        elif "bigquery.googleapis.com" in destination:
            dest_type = "athena_s3"
        elif "logging.googleapis.com" in destination:
            dest_type = "cloudwatch_log_group"
        elif "storage.googleapis.com" in destination:
            dest_type = "s3"

        sinks.append({
            "name":         name,
            "destination_type": dest_type,
            "log_group_filter_pattern": "",  # operator translates filter expression
            "_source_destination": destination,
            "_source_filter": filter_expr,
        })

    if not sinks:
        notes.append("No log sinks detected in source; emitted empty map.")
    else:
        notes.append(f"Emitted {len(sinks)} log sink(s) targeting Kinesis Firehose + S3.")
        notes.append("Filter expressions are NOT auto-translated — GCP uses LQL (Cloud Logging Query Language); "
                     "AWS CloudWatch Logs uses different syntax. See per-sink TODO comments.")

    aws_inputs_hcl = (
        "  # Translated from GCP google_logging_project_sink.\n"
        "  # GCP log routing → AWS pattern: CloudWatch Logs subscription filter\n"
        "  # → Kinesis Firehose → S3 (long-term storage; query via Athena).\n"
        f"  sinks = {_render_sinks(sinks)}\n"
        "\n"
        "  # TODO: wire to existing S3 bucket / KMS key for Firehose destination\n"
        '  destination_bucket = "TODO-firehose-destination-bucket"\n'
    )

    return Translation(
        service_name=SERVICE_NAME,
        aws_inputs_hcl=aws_inputs_hcl,
        notes=notes,
    )


def _render_sinks(sinks: list) -> str:
    if not sinks:
        return "{}"
    lines = ["{"]
    for s in sinks:
        key = s["name"].replace("-", "_").replace(".", "_")
        import re
        key = re.sub(r"\$\{[^}]*\}", "", key).strip("_") or "sink"
        lines.append(f'    "{key}" = {{')
        lines.append(f'      name              = "{s["name"]}"')
        lines.append(f'      destination_type  = "{s["destination_type"]}"')
        lines.append(f'      # source destination: {s["_source_destination"][:80]}')
        if s["_source_filter"]:
            short_filter = s["_source_filter"][:60].replace('"', "'")
            lines.append(f'      # source filter: {short_filter}')
            lines.append("      # TODO: translate filter expression to CloudWatch Logs filter pattern syntax")
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


_MAIN_TF = '''# AWS Kinesis Firehose log-sink module — emitted by Cloud Lifecycle Intelligence Migrator.
# Translates GCP google_logging_project_sink → CloudWatch Logs → Firehose → S3.

resource "aws_iam_role" "firehose" {
  name = "${var.name_prefix}-firehose-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "firehose.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })

  tags = var.tags
}

resource "aws_iam_role_policy" "firehose_s3" {
  role = aws_iam_role.firehose.name

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Action = [
        "s3:AbortMultipartUpload",
        "s3:GetBucketLocation",
        "s3:GetObject",
        "s3:ListBucket",
        "s3:ListBucketMultipartUploads",
        "s3:PutObject",
      ]
      Resource = [
        "arn:aws:s3:::${var.destination_bucket}",
        "arn:aws:s3:::${var.destination_bucket}/*",
      ]
    }]
  })
}

resource "aws_kinesis_firehose_delivery_stream" "this" {
  for_each = var.sinks

  name        = each.value.name
  destination = "extended_s3"

  extended_s3_configuration {
    role_arn   = aws_iam_role.firehose.arn
    bucket_arn = "arn:aws:s3:::${var.destination_bucket}"

    buffering_size     = 5
    buffering_interval = 60

    compression_format = "GZIP"
    prefix             = "logs/${each.value.name}/!{timestamp:yyyy/MM/dd}/"
    error_output_prefix = "errors/${each.value.name}/!{firehose:error-output-type}/!{timestamp:yyyy/MM/dd}/"
  }

  tags = merge(
    var.tags,
    { Name = each.value.name },
  )
}
'''


_VARIABLES_TF = '''variable "sinks" {
  type = map(object({
    name             = string
    destination_type = string  # firehose_to_s3, cloudwatch_log_group, athena_s3
  }))
  description = "Map of sink key -> spec."
  default     = {}
}

variable "destination_bucket" {
  type        = string
  description = "S3 bucket name for Firehose destination (for s3 / firehose_to_s3 sink types)."
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


_OUTPUTS_TF = '''output "firehose_arns" {
  value = { for k, f in aws_kinesis_firehose_delivery_stream.this : k => f.arn }
  description = "Map of sink key -> Kinesis Firehose ARN."
}

output "firehose_role_arn" {
  value = aws_iam_role.firehose.arn
  description = "IAM role ARN used by Firehose. Grant CloudWatch Logs subscription filter permission to invoke this role."
}
'''


_README = '''# AWS Log Sink (Firehose to S3) module

Translates GCP `google_logging_project_sink` to AWS:
- **Pub/Sub destination** → CloudWatch Logs subscription filter → Kinesis Firehose → S3
- **GCS destination** → CloudWatch Logs subscription filter → Firehose → S3 (similar)
- **BigQuery destination** → Firehose → S3 + Athena query layer

## What's NOT auto-translated

- **Filter expressions** — GCP uses LQL (Cloud Logging Query Language).
  AWS CloudWatch Logs uses metric filter syntax. Pattern translation
  needs to be done manually per sink (TODO markers in inputs).
- **Subscription filter wiring** — operator must add `aws_cloudwatch_log_subscription_filter`
  resources pointing CloudWatch Log Groups at the Firehose ARNs (out of scope here).

## Architecture pattern

```
CloudWatch Log Group → subscription filter → Kinesis Firehose → S3
                                                              → (optional) Athena for query
```

Compare to GCP:
```
Cloud Logging → log sink → Pub/Sub topic / GCS bucket / BQ dataset
```
'''
