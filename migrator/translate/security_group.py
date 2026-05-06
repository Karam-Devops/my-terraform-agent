"""GCP google_compute_firewall → AWS aws_security_group.

Source pattern:

    inputs = {
      firewall_rules = {
        "vpc-dev-shared" = {
          network = "vpc-dev-shared"
          ingress_rules = {
            "fw-allow-iap-ssh" = {
              description    = "..."
              priority       = 1000
              source_ranges  = ["35.235.240.0/20"]
              target_tags    = ["ssh"]
              allow = [
                { protocol = "tcp", ports = ["22"] }
              ]
            }
          }
          egress_rules = { ... }
        }
      }
    }

Topology shift: GCP firewall is VPC-attached (rules apply to entire VPC,
filtered by tag); AWS Security Group is instance-attached (rule applies
only to instances with the SG).

We emit one SG per source VPC + one SG rule per ingress/egress rule.
Operator must attach the right SG to the right instance.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List

from migrator.results import DiscoveredResource

from .base import DEFAULT_VERSIONS_TF, AWSModuleSpec, Translation


SERVICE_NAME = "security-group"


# Strip HCL interpolations from identifier-position strings (map keys
# can't contain `${...}`). Customer's source firewall_rules dict often
# uses `${local.X}` as the VPC-name key.
_INTERP_STRIP_RE = re.compile(r"\$\{[^}]*\}")


def _sanitize_identifier(s: str) -> str:
    """Convert an arbitrary string into a valid HCL identifier.

    Strips `${...}` interpolations, replaces non-alphanumeric chars
    with underscores, strips leading underscores, falls back to
    'sg' if empty.
    """
    s = _INTERP_STRIP_RE.sub("", s)
    s = re.sub(r"[^A-Za-z0-9_]", "_", s).strip("_")
    return s or "sg"


def translate(resource: DiscoveredResource) -> Translation:
    args = resource.arguments or {}
    notes: List[str] = []

    raw_rules = args.get("firewall_rules") or {}
    if not isinstance(raw_rules, dict):
        raw_rules = {}

    # Each top-level key is a VPC name. Collapse to {vpc_name: {ingress: [], egress: []}}
    sg_specs = []
    for vpc_name, fw_cfg in raw_rules.items():
        if not isinstance(fw_cfg, dict):
            continue

        ingress_src = fw_cfg.get("ingress_rules") or {}
        egress_src = fw_cfg.get("egress_rules") or {}

        ingress_rules = _collect_rules(ingress_src, "ingress") if isinstance(ingress_src, dict) else []
        egress_rules = _collect_rules(egress_src, "egress") if isinstance(egress_src, dict) else []

        # Sanitize the customer's vpc_name (often contains HCL
        # interpolations like ${local.vpc_config.vpc_name}). The key
        # has to be a valid identifier; the SG name field becomes a
        # generic "<sanitized>-sg".
        sanitized = _sanitize_identifier(str(vpc_name))
        sg_specs.append({
            "key":           sanitized,
            "vpc_name_safe": sanitized,
            "ingress_rules": ingress_rules,
            "egress_rules":  egress_rules,
        })

    if not sg_specs:
        notes.append("No firewall_rules detected in source; emitted empty map.")
    else:
        total_ingress = sum(len(s["ingress_rules"]) for s in sg_specs)
        total_egress = sum(len(s["egress_rules"]) for s in sg_specs)
        notes.append(f"Emitted {len(sg_specs)} SG(s) with {total_ingress} ingress + {total_egress} egress rules.")
        notes.append("TOPOLOGY SHIFT: GCP firewall is VPC-wide (filtered by `target_tags`); AWS SG attaches "
                     "to specific instances. Operator must associate the SG with the right instances + ENIs.")
        notes.append("`target_tags` from source are not directly translated — they're tracked in SG names "
                     "for reference but operator wires actual instance attachment.")

    aws_inputs_hcl = (
        "  # Translated from GCP google_compute_firewall.\n"
        f"  security_groups = {_render_sgs(sg_specs)}\n"
        "\n"
        "  # TODO: wire to vpc_id from networking module\n"
        '  vpc_id = "vpc-TODO"\n'
    )

    return Translation(
        service_name=SERVICE_NAME,
        aws_inputs_hcl=aws_inputs_hcl,
        notes=notes,
    )


def _collect_rules(rules_dict: Dict[str, Any], direction: str) -> List[Dict]:
    out = []
    for rule_name, rule in rules_dict.items():
        if not isinstance(rule, dict):
            continue
        sources = rule.get("source_ranges") or rule.get("destination_ranges") or []
        if not isinstance(sources, list):
            sources = [sources] if isinstance(sources, str) else []

        allow_blocks = rule.get("allow") or []
        if not isinstance(allow_blocks, list):
            allow_blocks = []

        # Each `allow` block has {protocol, ports[]}. Flatten to per-port-range rules.
        flat = []
        for ab in allow_blocks:
            if not isinstance(ab, dict):
                continue
            protocol = str(ab.get("protocol", "tcp")).lower()
            ports = ab.get("ports") or []
            if not isinstance(ports, list):
                ports = [ports]
            if not ports:
                # All ports
                flat.append({"protocol": protocol, "from_port": 0, "to_port": 65535})
                continue
            for p in ports:
                p_str = str(p)
                if "-" in p_str:
                    lo, hi = p_str.split("-", 1)
                    flat.append({"protocol": protocol, "from_port": int(lo), "to_port": int(hi)})
                else:
                    try:
                        port_num = int(p_str)
                        flat.append({"protocol": protocol, "from_port": port_num, "to_port": port_num})
                    except ValueError:
                        flat.append({"protocol": protocol, "from_port": 0, "to_port": 65535})

        if not flat:
            continue
        for f in flat:
            out.append({
                "name":        str(rule_name),
                "description": str(rule.get("description", ""))[:255],
                "protocol":    f["protocol"],
                "from_port":   f["from_port"],
                "to_port":     f["to_port"],
                "cidr_blocks": [str(s) for s in sources] or ["0.0.0.0/0"],
                "direction":   direction,
            })
    return out


def _render_sgs(sgs: list) -> str:
    if not sgs:
        return "{}"
    lines = ["{"]
    for s in sgs:
        key = s["key"]
        lines.append(f'    "{key}" = {{')
        lines.append(f'      name        = "${{local.environment}}-{key}-sg"')
        lines.append(f'      description = "Translated from GCP firewall rules for {key}"')

        if s["ingress_rules"]:
            lines.append("      ingress_rules = [")
            for r in s["ingress_rules"]:
                lines.append("        {")
                lines.append(f'          description = "{r["description"]}"')
                lines.append(f'          protocol    = "{r["protocol"]}"')
                lines.append(f"          from_port   = {r['from_port']}")
                lines.append(f"          to_port     = {r['to_port']}")
                cidrs = ", ".join(f'"{c}"' for c in r["cidr_blocks"])
                lines.append(f"          cidr_blocks = [{cidrs}]")
                lines.append("        },")
            lines.append("      ]")
        else:
            lines.append("      ingress_rules = []")

        if s["egress_rules"]:
            lines.append("      egress_rules = [")
            for r in s["egress_rules"]:
                lines.append("        {")
                lines.append(f'          description = "{r["description"]}"')
                lines.append(f'          protocol    = "{r["protocol"]}"')
                lines.append(f"          from_port   = {r['from_port']}")
                lines.append(f"          to_port     = {r['to_port']}")
                cidrs = ", ".join(f'"{c}"' for c in r["cidr_blocks"])
                lines.append(f"          cidr_blocks = [{cidrs}]")
                lines.append("        },")
            lines.append("      ]")
        else:
            lines.append("      egress_rules = []")

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


_MAIN_TF = '''# AWS Security Group module — emitted by Cloud Lifecycle Intelligence Migrator.
# Translates GCP google_compute_firewall.

resource "aws_security_group" "this" {
  for_each = var.security_groups

  name        = each.value.name
  description = each.value.description
  vpc_id      = var.vpc_id

  tags = merge(
    var.tags,
    { Name = each.value.name },
  )
}

locals {
  flat_ingress = flatten([
    for sk, sg in var.security_groups : [
      for i, r in sg.ingress_rules : {
        sg_key = sk
        rule_id = i
        rule    = r
      }
    ]
  ])
  flat_egress = flatten([
    for sk, sg in var.security_groups : [
      for i, r in sg.egress_rules : {
        sg_key = sk
        rule_id = i
        rule    = r
      }
    ]
  ])
}

resource "aws_security_group_rule" "ingress" {
  for_each = { for r in local.flat_ingress : "${r.sg_key}__ingress__${r.rule_id}" => r }

  type              = "ingress"
  security_group_id = aws_security_group.this[each.value.sg_key].id
  description       = each.value.rule.description
  protocol          = each.value.rule.protocol
  from_port         = each.value.rule.from_port
  to_port           = each.value.rule.to_port
  cidr_blocks       = each.value.rule.cidr_blocks
}

resource "aws_security_group_rule" "egress" {
  for_each = { for r in local.flat_egress : "${r.sg_key}__egress__${r.rule_id}" => r }

  type              = "egress"
  security_group_id = aws_security_group.this[each.value.sg_key].id
  description       = each.value.rule.description
  protocol          = each.value.rule.protocol
  from_port         = each.value.rule.from_port
  to_port           = each.value.rule.to_port
  cidr_blocks       = each.value.rule.cidr_blocks
}
'''


_VARIABLES_TF = '''variable "security_groups" {
  type = map(object({
    name        = string
    description = string
    ingress_rules = list(object({
      description = string
      protocol    = string
      from_port   = number
      to_port     = number
      cidr_blocks = list(string)
    }))
    egress_rules = list(object({
      description = string
      protocol    = string
      from_port   = number
      to_port     = number
      cidr_blocks = list(string)
    }))
  }))
  description = "Map of SG key -> spec."
  default     = {}
}

variable "vpc_id" {
  type        = string
  description = "VPC ID to attach SGs to."
}

variable "tags" {
  type    = map(string)
  default = {}
}
'''


_OUTPUTS_TF = '''output "security_group_ids" {
  value = { for k, sg in aws_security_group.this : k => sg.id }
  description = "Map of SG key -> ID. Attach these to instances/ENIs as needed."
}
'''


_README = '''# AWS Security Group module

Translates GCP `google_compute_firewall`. Each source firewall_rules
entry → one SG with N ingress + M egress rules.

## Topology shift you must reckon with

GCP firewall is VPC-wide (rules apply across every instance in the VPC,
filtered by `target_tags`). AWS Security Group is **instance-attached** —
the rule only applies to instances/ENIs that have the SG attached.

- Source `target_tags = ["ssh"]` was a way to scope a rule to specific
  instances. In AWS, you'd attach the SG to those instances directly.
- Source rules with no `target_tags` (apply to all instances) → attach
  the SG to all instances in the VPC manually, OR convert to a NACL on
  the subnet (works at the subnet level, like GCP firewall).

## What's not translated

- `priority` ordering — AWS SGs evaluate all rules; first-match doesn't
  apply. Higher priority in source becomes "narrower" in scope (TODO
  for operator review).
- `target_service_accounts` — not relevant for AWS (use IAM roles instead).
- DENY rules — AWS SGs are allow-only. DENY logic must move to NACLs
  (subnet level) or be expressed as "exclude these CIDRs from the
  allow list."
'''
