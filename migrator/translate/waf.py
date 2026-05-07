"""GCP google_compute_security_policy (Cloud Armor) → AWS aws_wafv2_web_acl.

Source pattern:

    inputs = {
      cloud_armor_config = [
        {
          name = "dh-security-policy-${local.env}"
          default_rule_action = "allow"
          layer_7_ddos_defense_enable = true
          pre_configured_rules = {
            "sqli-stable-rule" = {
              action = "deny(403)"
              priority = 2500
              target_rule_set = "sqli-v33-stable"
            }
          }
        }
      ]
    }

AWS WAF v2 differs significantly:
- WAF rules use Statement DSL (JSON) instead of Cloud Armor's CEL
- Pre-configured OWASP rule sets are in AWS Managed Rules (free)
- DDoS protection in AWS is AWS Shield (separate service)
"""

from __future__ import annotations

from typing import Any, Dict, List

from migrator.results import DiscoveredResource

from .base import DEFAULT_VERSIONS_TF, AWSModuleSpec, Translation


SERVICE_NAME = "wafv2-web-acl"


# Map Cloud Armor's pre-configured rule names to AWS WAF Managed Rule Group names.
_RULESET_MAP = {
    "sqli-v33-stable":         "AWSManagedRulesSQLiRuleSet",
    "sqli-stable-rule":        "AWSManagedRulesSQLiRuleSet",
    "xss-v33-stable":          "AWSManagedRulesCommonRuleSet",
    "xss-stable-rule":         "AWSManagedRulesCommonRuleSet",
    "lfi-stable":              "AWSManagedRulesLinuxRuleSet",
    "rfi-stable":              "AWSManagedRulesLinuxRuleSet",
    "scannerdetection-stable": "AWSManagedRulesAdminProtectionRuleSet",
    "protocolattack-stable":   "AWSManagedRulesCommonRuleSet",
    "rce-stable":              "AWSManagedRulesCommonRuleSet",
    "methodenforcement-stable": "AWSManagedRulesCommonRuleSet",
    "sessionfixation-stable":  "AWSManagedRulesCommonRuleSet",
    "java-v33-stable":         "AWSManagedRulesAnonymousIpList",
    "nodejs-v33-stable":       "AWSManagedRulesAnonymousIpList",
}


def translate(resource: DiscoveredResource) -> Translation:
    args = resource.arguments or {}
    notes: List[str] = []

    raw_policies = args.get("cloud_armor_config") or args.get("policies") or []
    if not isinstance(raw_policies, list):
        raw_policies = []

    web_acls = []
    for src in raw_policies:
        if not isinstance(src, dict):
            continue

        name = str(src.get("name", "TODO-waf-acl"))
        default_action = str(src.get("default_rule_action", "allow")).lower()
        if default_action not in ("allow", "block"):
            default_action = "allow"
        # Map "deny" → "block" (AWS terminology)
        default_action = "block" if default_action == "deny" else default_action

        ddos_enable = bool(src.get("layer_7_ddos_defense_enable", False))
        if ddos_enable:
            notes.append(
                f"Cloud Armor `{name}` has layer_7_ddos_defense_enable=true. "
                "AWS DDoS protection is AWS Shield (separate service) — "
                "this WAF ACL emits AWS Managed Rules but Shield Advanced "
                "needs to be enabled at the AWS account level (out of scope for this module)."
            )

        # Translate pre-configured rules to AWS Managed Rule Groups.
        pre_rules = src.get("pre_configured_rules") or {}
        managed_rules = []
        custom_rules = []
        if isinstance(pre_rules, dict):
            for rule_name, rule in pre_rules.items():
                if not isinstance(rule, dict):
                    continue
                action = str(rule.get("action", "deny(403)")).lower()
                priority = int(rule.get("priority", 1000) or 1000)
                target_rule_set = str(rule.get("target_rule_set", ""))
                aws_managed_group = _RULESET_MAP.get(target_rule_set, "AWSManagedRulesCommonRuleSet")

                action_aws = "block" if "deny" in action else "allow"
                managed_rules.append({
                    "name":        str(rule_name),
                    "priority":    priority,
                    "managed_group": aws_managed_group,
                    "action":      action_aws,
                })

        # Also allow any custom_rules from source (rate-limiting, geo-blocking, etc.)
        custom_rules_src = src.get("custom_rules") or {}
        if isinstance(custom_rules_src, dict):
            for rule_name, rule in custom_rules_src.items():
                if not isinstance(rule, dict):
                    continue
                priority = int(rule.get("priority", 5000) or 5000)
                action = str(rule.get("action", "allow")).lower()
                action_aws = "block" if "deny" in action else "allow"
                custom_rules.append({
                    "name": str(rule_name),
                    "priority": priority,
                    "action": action_aws,
                    "_source": rule,
                })

        web_acls.append({
            "name":           name,
            "scope":          "REGIONAL",   # default; CLOUDFRONT scope = global ALB
            "default_action": default_action,
            "managed_rules":  managed_rules,
            "custom_rules":   custom_rules,
        })

    if not web_acls:
        notes.append("No cloud_armor_config detected in source; emitted empty list.")
    else:
        notes.append(f"Emitted {len(web_acls)} WAF v2 Web ACL(s).")
        notes.append("Cloud Armor → WAF: pre-configured OWASP rules map to AWS Managed Rule Groups (free). "
                     "Custom CEL expressions need rewriting as AWS WAF Statement DSL (JSON) — flagged inline.")
        notes.append("scope = REGIONAL applies to ALB / API Gateway. For CloudFront use scope = CLOUDFRONT "
                     "and deploy in us-east-1 only.")

    aws_inputs_hcl = (
        "  # Translated from GCP google_compute_security_policy (Cloud Armor).\n"
        f"  web_acls = {_render_acls(web_acls)}\n"
    )

    return Translation(
        service_name=SERVICE_NAME,
        aws_inputs_hcl=aws_inputs_hcl,
        notes=notes,
    )


def _render_acls(acls: list) -> str:
    if not acls:
        return "{}"
    lines = ["{"]
    for a in acls:
        key = a["name"].replace("-", "_").replace(".", "_")
        # Sanitize HCL interpolations from key
        import re
        key = re.sub(r"\$\{[^}]*\}", "", key).strip("_") or "waf_acl"
        lines.append(f'    "{key}" = {{')
        lines.append(f'      name           = "{a["name"]}"')
        lines.append(f'      scope          = "{a["scope"]}"')
        lines.append(f'      default_action = "{a["default_action"]}"')
        if a["managed_rules"]:
            lines.append("      managed_rules = [")
            for r in a["managed_rules"]:
                lines.append("        {")
                lines.append(f'          name           = "{r["name"]}"')
                lines.append(f'          priority       = {r["priority"]}')
                lines.append(f'          managed_group  = "{r["managed_group"]}"')
                lines.append(f'          action         = "{r["action"]}"')
                lines.append("        },")
            lines.append("      ]")
        else:
            lines.append("      managed_rules = []")
        if a["custom_rules"]:
            lines.append("      custom_rules = [")
            for r in a["custom_rules"]:
                lines.append("        {")
                lines.append(f'          name     = "{r["name"]}"')
                lines.append(f'          priority = {r["priority"]}')
                lines.append(f'          action   = "{r["action"]}"')
                lines.append("          # TODO: translate Cloud Armor CEL expression to AWS WAF Statement DSL")
                lines.append("        },")
            lines.append("      ]")
        else:
            lines.append("      custom_rules = []")
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


_MAIN_TF = '''# AWS WAF v2 Web ACL module — emitted by Cloud Lifecycle Intelligence Migrator.
# Translates GCP Cloud Armor security policies.

resource "aws_wafv2_web_acl" "this" {
  for_each = var.web_acls

  name  = each.value.name
  scope = each.value.scope

  default_action {
    dynamic "allow" {
      for_each = each.value.default_action == "allow" ? [1] : []
      content {}
    }
    dynamic "block" {
      for_each = each.value.default_action == "block" ? [1] : []
      content {}
    }
  }

  dynamic "rule" {
    for_each = each.value.managed_rules
    content {
      name     = rule.value.name
      priority = rule.value.priority

      override_action {
        none {}
      }

      statement {
        managed_rule_group_statement {
          name        = rule.value.managed_group
          vendor_name = "AWS"
        }
      }

      visibility_config {
        cloudwatch_metrics_enabled = true
        metric_name                = replace(rule.value.name, "-", "_")
        sampled_requests_enabled   = true
      }
    }
  }

  visibility_config {
    cloudwatch_metrics_enabled = true
    metric_name                = replace(each.value.name, "-", "_")
    sampled_requests_enabled   = true
  }

  tags = merge(
    var.tags,
    { Name = each.value.name },
  )
}
'''


_VARIABLES_TF = '''variable "web_acls" {
  type = map(object({
    name           = string
    scope          = string  # REGIONAL (ALB/API GW) or CLOUDFRONT (must be us-east-1)
    default_action = string  # "allow" or "block"
    managed_rules = list(object({
      name          = string
      priority      = number
      managed_group = string  # e.g. "AWSManagedRulesCommonRuleSet"
      action        = string  # "allow" or "block"
    }))
    custom_rules = list(object({
      name     = string
      priority = number
      action   = string
    }))
  }))
  description = "Map of WAF ACL key -> spec."
  default     = {}
}

variable "tags" {
  type    = map(string)
  default = {}
}
'''


_OUTPUTS_TF = '''output "web_acl_arns" {
  value = { for k, w in aws_wafv2_web_acl.this : k => w.arn }
  description = "Map of WAF ACL key -> ARN. Associate with ALB/CloudFront via aws_wafv2_web_acl_association."
}
'''


_README = '''# AWS WAF v2 Web ACL module

Translates GCP Cloud Armor security policies to AWS WAF v2.

## OWASP rule mapping

Cloud Armor's pre-configured rules → AWS Managed Rule Groups (all free):

| Cloud Armor preset | AWS Managed Rule Group |
|---|---|
| sqli-v33-stable | AWSManagedRulesSQLiRuleSet |
| xss-v33-stable | AWSManagedRulesCommonRuleSet |
| lfi-stable, rfi-stable | AWSManagedRulesLinuxRuleSet |
| rce-stable, protocolattack-stable, methodenforcement-stable | AWSManagedRulesCommonRuleSet |
| scannerdetection-stable | AWSManagedRulesAdminProtectionRuleSet |

## What's NOT auto-translated

- **Custom CEL expressions** — Cloud Armor's expression DSL (`request.path matches "..."`) needs hand-rewriting to AWS WAF Statement JSON.
- **Geo-blocking** — translate to AWS WAF `GeoMatchStatement` separately.
- **Rate limiting** — translate to AWS WAF `RateBasedStatement` separately.
- **Layer 7 DDoS defense** — AWS equivalent is AWS Shield Advanced (different service, account-level enable).

## Required follow-up

- Associate WAF ACL with ALB / API Gateway / CloudFront via `aws_wafv2_web_acl_association`.
- For CloudFront: WAF ACL must be `scope = "CLOUDFRONT"` and deployed in `us-east-1`.
'''
