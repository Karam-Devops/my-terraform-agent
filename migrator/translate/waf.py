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

import re
from typing import Any, Dict, List, Optional

from migrator.results import DiscoveredResource

from .base import DEFAULT_VERSIONS_TF, AWSModuleSpec, Translation


SERVICE_NAME = "wafv2-web-acl"


# CEL expression patterns we know how to translate. Order matters:
# more specific patterns first (e.g., geo-block before generic match).

# Geo-block pattern: 'CC,CC,CC'.contains(origin.region_code)
# Example from CitiusTech: '[RU,UA,CN,KR,LV,MD,KP,TW]'.contains(origin.region_code)
_GEO_MATCH_RE = re.compile(
    r"""['"]?\[?([A-Z][A-Z](?:\s*,\s*[A-Z][A-Z])*)\]?['"]?\s*\.contains\s*\(\s*origin\.region_code\s*\)""",
    re.IGNORECASE,
)

# Rate-limit pattern: rate_limit_options at the source rule level
# (already a dict, not a CEL expression — handled separately).


def _try_translate_cel_to_aws_statement(expression: str) -> Optional[Dict[str, Any]]:
    """Best-effort translation of a Cloud Armor CEL expression to an
    AWS WAF Statement JSON structure.

    Returns a dict like:
      { "kind": "geo_match", "country_codes": ["RU", "UA", ...] }
    when the pattern is recognized; None otherwise. The renderer turns
    this into HCL.

    Today: only geo-match patterns. Other CEL constructs (path matches,
    header inspection, source IP ranges) need follow-up extensions.
    """
    if not isinstance(expression, str) or not expression:
        return None

    # Try geo-match pattern
    m = _GEO_MATCH_RE.search(expression)
    if m:
        codes_str = m.group(1)
        codes = [c.strip().upper() for c in codes_str.split(",") if c.strip()]
        # Sanity-check: ISO 3166-1 alpha-2 codes are exactly 2 chars
        codes = [c for c in codes if len(c) == 2]
        if codes:
            return {"kind": "geo_match", "country_codes": codes}

    return None


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

                # Try to translate the CEL expression to a known AWS WAF
                # Statement shape (today: geo-match).
                expression = (
                    rule.get("expression")
                    or rule.get("match", {}).get("expression")
                    or ""
                )
                aws_statement = _try_translate_cel_to_aws_statement(str(expression))
                if aws_statement:
                    notes.append(
                        f"WAF rule `{rule_name}` translated: CEL expression "
                        f"recognized as {aws_statement['kind']} pattern "
                        f"({len(aws_statement.get('country_codes', []))} country codes)."
                    )

                custom_rules.append({
                    "name": str(rule_name),
                    "priority": priority,
                    "action": action_aws,
                    "aws_statement": aws_statement,   # None when not translated
                    "_source_expression": expression,
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
                # Emit translated AWS WAF Statement when CEL was recognized.
                aws_stmt = r.get("aws_statement")
                if aws_stmt and aws_stmt.get("kind") == "geo_match":
                    codes = ", ".join(f'"{c}"' for c in aws_stmt["country_codes"])
                    lines.append(f'          statement_kind  = "geo_match"')
                    lines.append(f'          country_codes   = [{codes}]')
                else:
                    lines.append('          statement_kind  = "todo"')
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

  # ---- Managed Rule Groups (AWS-curated OWASP / SQLi / etc.) ----
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

  # ---- Custom Rules (translated from Cloud Armor CEL expressions) ----
  # Today: geo_match_statement when source CEL was
  # '[CC,CC,...]'.contains(origin.region_code). Other CEL constructs
  # land as `statement_kind = "todo"` with operator-facing comments.
  dynamic "rule" {
    for_each = [
      for r in each.value.custom_rules :
      r if lookup(r, "statement_kind", "") == "geo_match"
    ]
    content {
      name     = rule.value.name
      priority = rule.value.priority

      action {
        dynamic "block" {
          for_each = rule.value.action == "block" ? [1] : []
          content {}
        }
        dynamic "allow" {
          for_each = rule.value.action == "allow" ? [1] : []
          content {}
        }
      }

      statement {
        geo_match_statement {
          country_codes = rule.value.country_codes
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


# `web_acls` declared as map(any) because custom_rules entries are
# heterogeneous: geo_match rules carry country_codes; todo rules don't.
# Strict map(object(...)) would fail type-inference on that variance.
# Implicit schema documented in the translator source.
_VARIABLES_TF = '''variable "web_acls" {
  type        = map(any)
  description = "Map of WAF ACL key -> spec. Schema in translator source."
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
