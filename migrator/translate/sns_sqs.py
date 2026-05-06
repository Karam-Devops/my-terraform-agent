"""GCP google_pubsub_topic / google_pubsub_subscription → AWS SNS topic + SQS queue(s).

Source pattern (Pub/Sub):

    inputs = {
      pubsub_config = {
        "cdc-bucket" = {
          name = "${local._project.locals.project_id}-shared-cdc-topic"
          subscriptions = {
            "shared-cdc-subscription" = {
              enabled        = true
              payload_format = "JSON_API_V1"
              iam_bindings = { ... }
            }
          }
        }
      }
    }

Topology shift: GCP combines topic + subscriptions in one resource family.
AWS has SNS topics (publish) + SQS queues (subscribers) connected via
SNS-SQS subscription. Each GCP subscription becomes an SQS queue subscribed
to the SNS topic.
"""

from __future__ import annotations

from typing import List

from migrator.results import DiscoveredResource

from .base import DEFAULT_VERSIONS_TF, AWSModuleSpec, Translation


SERVICE_NAME = "sns-sqs-fanout"


def translate(resource: DiscoveredResource) -> Translation:
    args = resource.arguments or {}
    notes: List[str] = []

    pubsub_config = args.get("pubsub_config") or args.get("topics") or {}
    if not isinstance(pubsub_config, dict):
        pubsub_config = {}

    topics_out = []
    for topic_key, topic_spec in pubsub_config.items():
        if not isinstance(topic_spec, dict):
            continue
        topic_name = str(topic_spec.get("name", topic_key))
        subs = topic_spec.get("subscriptions") or {}
        if not isinstance(subs, dict):
            subs = {}

        sub_specs = []
        for sub_key, sub_cfg in subs.items():
            if not isinstance(sub_cfg, dict):
                sub_cfg = {}
            sub_specs.append({
                "name":    str(sub_key),
                "enabled": bool(sub_cfg.get("enabled", True)),
            })

        topics_out.append({
            "key":   str(topic_key),
            "name":  topic_name,
            "subscriptions": sub_specs,
        })

    if not topics_out and isinstance(args.get("name"), str):
        # Single-topic input shape (older Pub/Sub modules)
        topics_out.append({
            "key":   "default",
            "name":  args["name"],
            "subscriptions": [],
        })

    if not topics_out:
        notes.append("No pubsub_config detected in source; emitted empty topics map.")
    else:
        notes.append(f"Emitted {len(topics_out)} SNS topic(s) + "
                     f"{sum(len(t['subscriptions']) for t in topics_out)} SQS subscriber queue(s).")
        notes.append("Topology shift: GCP topic+subscription is one resource family; "
                     "AWS splits into SNS (publish) + SQS (subscriber). Subscriptions become SQS queues.")
        notes.append("In-flight message replay during cutover: drain GCP subscription to zero first, "
                     "then switch publishers to SNS. See migration_helpers/06-pubsub-to-sns-sqs-replay.md.")
        notes.append("Filter expressions / ordering keys: GCP and AWS use different syntax — review per subscription.")

    aws_inputs_hcl = (
        "  # Translated from GCP pubsub_config (topics + subscriptions).\n"
        "  # Each topic becomes one SNS topic; each subscription becomes one SQS queue\n"
        "  # subscribed to that topic.\n"
        f"  topics = {_render_topics(topics_out)}\n"
    )

    return Translation(
        service_name=SERVICE_NAME,
        aws_inputs_hcl=aws_inputs_hcl,
        notes=notes,
    )


def _render_topics(topics: list) -> str:
    if not topics:
        return "{}"
    lines = ["{"]
    for t in topics:
        key = t["key"].replace("-", "_").replace(".", "_")
        lines.append(f'    "{key}" = {{')
        lines.append(f'      name = "{t["name"]}"')
        if t["subscriptions"]:
            lines.append("      subscriptions = {")
            for s in t["subscriptions"]:
                skey = s["name"].replace("-", "_").replace(".", "_")
                lines.append(f'        "{skey}" = {{')
                lines.append(f'          name    = "{s["name"]}"')
                lines.append(f'          enabled = {str(s["enabled"]).lower()}')
                lines.append("        }")
            lines.append("      }")
        else:
            lines.append("      subscriptions = {}")
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


_MAIN_TF = '''# AWS SNS+SQS fan-out module — emitted by Cloud Lifecycle Intelligence Migrator.
# Translates GCP Pub/Sub topics + subscriptions to AWS SNS topics + SQS queues
# subscribed via sns-sqs subscriptions (the standard fan-out pattern).

locals {
  flattened_subscriptions = flatten([
    for tk, t in var.topics : [
      for sk, s in t.subscriptions : {
        topic_key = tk
        sub_key   = sk
        sub       = s
      } if s.enabled
    ]
  ])
}

resource "aws_sns_topic" "this" {
  for_each = var.topics
  name     = each.value.name
  tags     = var.tags
}

resource "aws_sqs_queue" "this" {
  for_each = {
    for s in local.flattened_subscriptions :
    "${s.topic_key}__${s.sub_key}" => s
  }

  name                       = each.value.sub.name
  visibility_timeout_seconds = 60
  message_retention_seconds  = 1209600   # 14 days max
  sqs_managed_sse_enabled    = true

  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.dlq[each.key].arn
    maxReceiveCount     = 5
  })

  tags = var.tags
}

resource "aws_sqs_queue" "dlq" {
  for_each = {
    for s in local.flattened_subscriptions :
    "${s.topic_key}__${s.sub_key}" => s
  }

  name                       = "${each.value.sub.name}-dlq"
  message_retention_seconds  = 1209600
  sqs_managed_sse_enabled    = true

  tags = var.tags
}

resource "aws_sns_topic_subscription" "this" {
  for_each = {
    for s in local.flattened_subscriptions :
    "${s.topic_key}__${s.sub_key}" => s
  }

  topic_arn = aws_sns_topic.this[each.value.topic_key].arn
  protocol  = "sqs"
  endpoint  = aws_sqs_queue.this[each.key].arn
}

# Allow SNS to publish to the queues.
resource "aws_sqs_queue_policy" "allow_sns" {
  for_each = aws_sqs_queue.this

  queue_url = each.value.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "sns.amazonaws.com" }
      Action    = "sqs:SendMessage"
      Resource  = each.value.arn
      Condition = {
        ArnEquals = {
          "aws:SourceArn" = aws_sns_topic.this[split("__", each.key)[0]].arn
        }
      }
    }]
  })
}
'''


_VARIABLES_TF = '''variable "topics" {
  type = map(object({
    name = string
    subscriptions = map(object({
      name    = string
      enabled = bool
    }))
  }))
  description = "Map of topic key -> {name, subscriptions{}}. Each subscription becomes an SQS queue."
  default     = {}
}

variable "tags" {
  type    = map(string)
  default = {}
}
'''


_OUTPUTS_TF = '''output "topic_arns" {
  value       = { for k, t in aws_sns_topic.this : k => t.arn }
  description = "Map of topic key -> SNS topic ARN."
}

output "queue_arns" {
  value       = { for k, q in aws_sqs_queue.this : k => q.arn }
  description = "Map of <topic>__<sub> -> SQS queue ARN."
}

output "queue_urls" {
  value       = { for k, q in aws_sqs_queue.this : k => q.url }
  description = "Map of <topic>__<sub> -> SQS queue URL (used by consumers)."
}
'''


_README = '''# AWS SNS + SQS Fan-out module

Translates GCP `google_pubsub_topic` + `google_pubsub_subscription`. Each
GCP topic becomes an SNS topic. Each subscription becomes an SQS queue
that's subscribed to the SNS topic, with a per-queue dead-letter queue.

## Topology

```
GCP:
  Topic ─┬─ Subscription A
         └─ Subscription B

AWS:
  SNS Topic ─┬─ SQS Queue A (+ DLQ)
             └─ SQS Queue B (+ DLQ)
```

## Cutover notes

In-flight messages are NOT migrated automatically:
1. Stop publishing to the GCP Pub/Sub topic (or dual-publish to both).
2. Drain the GCP subscriptions to zero (verify with `gcloud pubsub subscriptions describe`).
3. Switch publishers to the AWS SNS topic.

See `migration_helpers/06-pubsub-to-sns-sqs-replay.md` for the full runbook.

## Differences worth flagging

- GCP push subscriptions → SNS HTTP/HTTPS or Lambda subscriptions (not SQS).
  This module emits SQS only; switch protocol manually if you need push.
- GCP filter expressions don't translate 1:1 to AWS SNS message filters —
  review per-subscription.
- GCP ordering keys → SQS FIFO + message group IDs (different SKU).
'''
