"""GCP google_cloud_scheduler_job → AWS aws_scheduler_schedule (EventBridge Scheduler).

Source pattern:

    inputs = {
      scheduler_jobs = [
        {
          name        = "startvm-automate"
          description = null
          schedule    = "00 08 * * 1-5"
          time_zone   = "Asia/Calcutta"
          pubsub_target = {
            topic_name = "..."
            data       = "..."
          }
          # OR http_target = { uri, http_method, body, headers, oauth_token }
        }
      ]
    }

EventBridge Scheduler is the modern AWS equivalent (released 2022),
replacing CloudWatch Events Rules for scheduled invocations. Targets
include Lambda, ECS tasks, SNS, SQS, and EventBridge buses.
"""

from __future__ import annotations

from typing import List

from migrator.results import DiscoveredResource

from .base import DEFAULT_VERSIONS_TF, AWSModuleSpec, Translation


SERVICE_NAME = "eventbridge-scheduler"


def translate(resource: DiscoveredResource) -> Translation:
    args = resource.arguments or {}
    notes: List[str] = []

    raw_jobs = args.get("scheduler_jobs") or args.get("jobs") or []
    if not isinstance(raw_jobs, list):
        raw_jobs = []

    schedules = []
    for src in raw_jobs:
        if not isinstance(src, dict):
            continue
        name = str(src.get("name", "TODO-schedule"))
        description = str(src.get("description") or "")
        schedule = str(src.get("schedule", "0 0 * * *"))
        time_zone = str(src.get("time_zone", "UTC"))

        # Detect target type
        target_type = "lambda"
        target_arn = "TODO-target-arn"
        target_payload = ""

        pubsub_target = src.get("pubsub_target")
        http_target = src.get("http_target")
        appengine_target = src.get("app_engine_http_target")

        if isinstance(pubsub_target, dict):
            target_type = "sns"
            target_arn = f"# arn:aws:sns:<region>:<account>:{pubsub_target.get('topic_name', 'TODO')}"
            data = pubsub_target.get("data", "")
            if data:
                target_payload = str(data)
            notes.append(
                f"job `{name}` had GCP Pub/Sub target → translated to SNS topic. "
                "Update target_arn to your migrated SNS topic ARN."
            )
        elif isinstance(http_target, dict):
            target_type = "http_endpoint"
            target_arn = f"# {http_target.get('uri', 'TODO')}"
            notes.append(
                f"job `{name}` had GCP HTTP target → use EventBridge connection + API destination "
                "or Lambda proxy."
            )
        elif isinstance(appengine_target, dict):
            target_type = "lambda"
            notes.append(
                f"job `{name}` had GCP App Engine target → translate to Lambda invocation. "
                "App Engine has no AWS equivalent; redeploy app code as Lambda."
            )

        # Translate cron format. GCP uses 5-field cron: m h d M dow
        # AWS EventBridge uses 6-field cron: m h d M dow y
        # We'll prepend 0 for seconds-position is wrong direction. AWS Scheduler's
        # cron is 6-field with year at end. Append "*" if we have 5 fields.
        cron_parts = schedule.split()
        if len(cron_parts) == 5:
            aws_schedule = f"cron({schedule} *)"
        elif len(cron_parts) == 6:
            aws_schedule = f"cron({schedule})"
        else:
            aws_schedule = f"cron({schedule})"
            notes.append(f"job `{name}`: unusual schedule format `{schedule}` — review manually.")

        schedules.append({
            "name":         name,
            "description":  description,
            "schedule":     aws_schedule,
            "time_zone":    time_zone,
            "target_type":  target_type,
            "target_arn":   target_arn,
            "input":        target_payload,
        })

    if not schedules:
        notes.append("No scheduler_jobs detected in source; emitted empty map.")
    else:
        notes.append(f"Emitted {len(schedules)} EventBridge Scheduler entries.")
        notes.append("Cron schedule conversion: GCP 5-field → AWS 6-field cron expression "
                     "(year field appended as '*'). Verify timezone semantics match.")

    aws_inputs_hcl = (
        "  # Translated from GCP google_cloud_scheduler_job.\n"
        f"  schedules = {_render_schedules(schedules)}\n"
    )

    return Translation(
        service_name=SERVICE_NAME,
        aws_inputs_hcl=aws_inputs_hcl,
        notes=notes,
    )


def _render_schedules(schedules: list) -> str:
    if not schedules:
        return "{}"
    lines = ["{"]
    for s in schedules:
        key = s["name"].replace("-", "_").replace(".", "_")
        import re
        key = re.sub(r"\$\{[^}]*\}", "", key).strip("_") or "schedule"
        lines.append(f'    "{key}" = {{')
        lines.append(f'      name        = "{s["name"]}"')
        if s["description"]:
            lines.append(f'      description = "{s["description"]}"')
        else:
            lines.append('      description = ""')
        lines.append(f'      schedule    = "{s["schedule"]}"')
        lines.append(f'      time_zone   = "{s["time_zone"]}"')
        lines.append(f'      target_type = "{s["target_type"]}"   # operator wires real target ARN below')
        if s["target_arn"].startswith("#"):
            lines.append(f'      target_arn  = "TODO-target-arn"  {s["target_arn"]}')
        else:
            lines.append(f'      target_arn  = "{s["target_arn"]}"')
        if s["input"]:
            # Customer's input payload often has embedded JSON with quotes
            # (e.g., `{"operation": "start"}`). Manual escaping is fragile
            # and python-hcl2's behavior on escapes varies. Safer: leave
            # input empty and put the customer's payload in a comment so
            # the operator can paste it back as a properly-escaped HCL
            # string or use a heredoc.
            short = s["input"][:80].replace('"', "'").replace("\n", " ")
            lines.append(f'      input       = ""  # TODO: source payload was: {short}')
        else:
            lines.append('      input       = ""')
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


_MAIN_TF = '''# AWS EventBridge Scheduler module — emitted by Cloud Lifecycle Intelligence Migrator.
# Translates GCP google_cloud_scheduler_job.

resource "aws_iam_role" "scheduler" {
  name = "${var.name_prefix}-scheduler-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "scheduler.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })

  tags = var.tags
}

# Generic permissive policy — operator should narrow per actual targets.
resource "aws_iam_role_policy" "scheduler_invoke" {
  role = aws_iam_role.scheduler.name
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Action = [
        "lambda:InvokeFunction",
        "sns:Publish",
        "sqs:SendMessage",
        "states:StartExecution",
      ]
      Resource = "*"
    }]
  })
}

resource "aws_scheduler_schedule" "this" {
  for_each = var.schedules

  name        = each.value.name
  description = each.value.description

  schedule_expression          = each.value.schedule
  schedule_expression_timezone = each.value.time_zone

  flexible_time_window {
    mode = "OFF"
  }

  target {
    arn      = each.value.target_arn
    role_arn = aws_iam_role.scheduler.arn
    input    = each.value.input
  }
}
'''


_VARIABLES_TF = '''variable "schedules" {
  type = map(object({
    name        = string
    description = string
    schedule    = string  # cron(m h d M dow y) or rate(N units)
    time_zone   = string
    target_type = string  # informational: lambda, sns, sqs, http_endpoint
    target_arn  = string
    input       = string  # JSON payload sent to target
  }))
  description = "Map of schedule key -> spec."
  default     = {}
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


_OUTPUTS_TF = '''output "schedule_arns" {
  value = { for k, s in aws_scheduler_schedule.this : k => s.arn }
  description = "Map of schedule key -> EventBridge Scheduler ARN."
}
'''


_README = '''# AWS EventBridge Scheduler module

Translates GCP `google_cloud_scheduler_job` to EventBridge Scheduler
(modern AWS scheduling, replacing CloudWatch Events for time-based triggers).

## GCP→AWS target mapping

| GCP target | AWS target |
|---|---|
| `pubsub_target` | SNS topic (post-Pub/Sub→SNS migration) |
| `http_target` | EventBridge API Destination + connection (or Lambda proxy) |
| `app_engine_http_target` | Lambda (App Engine has no AWS equivalent) |

## Cron format conversion

| GCP (5-field cron) | AWS EventBridge Scheduler (6-field cron) |
|---|---|
| `0 8 * * 1-5` (weekdays at 8 AM) | `cron(0 8 * * 1-5 *)` (year field added) |
| `0 22 * * 1-5` | `cron(0 22 * * 1-5 *)` |

Time zones: AWS uses IANA names (e.g., `Asia/Calcutta`, `America/New_York`).
GCP uses the same — pass-through works.

## Required follow-up per schedule

- Replace `target_arn = "TODO-target-arn"` with actual SNS / Lambda / SQS ARN.
- Narrow IAM policy on `aws_iam_role.scheduler` to only the targets actually invoked.
- Verify cron timing in non-UTC time zones (DST handling).
'''
