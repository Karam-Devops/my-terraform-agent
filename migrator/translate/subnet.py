"""GCP google_compute_subnetwork → AWS aws_subnet.

Distinct from `vpc.py` (which creates the VPC + its subnets together).
This translator handles the case where a stack ONLY adds subnets to
an existing VPC (the customer's `net-subnets` pattern).

Source pattern:

    inputs = {
      network    = "${local.vpc_config.vpc_name}"
      project_id = "${local.common_networking_project_id}"
      subnets = [
        { name, ip_cidr_range, region, secondary_ip_ranges, ... }
      ]
      subnets_psc = [...]   # GCP Private Service Connect subnets — no AWS analog
    }
"""

from __future__ import annotations

from typing import List

from migrator.results import DiscoveredResource

from .base import DEFAULT_VERSIONS_TF, AWSModuleSpec, Translation


SERVICE_NAME = "subnet"


def translate(resource: DiscoveredResource) -> Translation:
    args = resource.arguments or {}
    notes: List[str] = []

    raw_subnets = args.get("subnets") or []
    if not isinstance(raw_subnets, list):
        raw_subnets = []

    subnets = []
    for s in raw_subnets:
        if not isinstance(s, dict):
            continue
        sname = str(s.get("name", "TODO-subnet"))
        cidr = str(s.get("ip_cidr_range") or s.get("cidr") or "10.0.1.0/24")
        region = str(s.get("region", "us-east-1"))

        secondaries = s.get("secondary_ip_ranges") or []
        sec_ranges = []
        if isinstance(secondaries, list):
            for sr in secondaries:
                if isinstance(sr, dict):
                    sec_ranges.append({
                        "name": str(sr.get("range_name", "secondary")),
                        "cidr": str(sr.get("ip_cidr_range", "10.1.0.0/16")),
                    })

        subnets.append({
            "name":   sname,
            "cidr":   cidr,
            "region": region,
            "secondaries": sec_ranges,
            "private_google_access": bool(s.get("private_ip_google_access", False)),
        })

    # PSC subnets — note them but don't translate (no direct AWS analog)
    psc_subnets = args.get("subnets_psc") or []
    if isinstance(psc_subnets, list) and psc_subnets:
        notes.append(
            f"Source has {len(psc_subnets)} PSC subnet(s) (subnets_psc). "
            "GCP Private Service Connect has no direct AWS subnet analog — "
            "translates to PrivateLink/VPC Endpoint at the consumer side, separate workstream."
        )

    if not subnets:
        notes.append("No `subnets` list in source; emitted empty map.")
    else:
        total_secs = sum(len(s["secondaries"]) for s in subnets)
        notes.append(f"Emitted {len(subnets)} subnet entries ({total_secs} secondary IP ranges).")
        notes.append("AWS subnets are AZ-scoped, GCP subnets are region-scoped. "
                     "For HA in AWS, each source subnet should map to 2-3 AWS subnets across AZs — "
                     "operator decides per env.")
        notes.append("Secondary IP ranges (GCP, used for GKE pods/services) → additional CIDR blocks "
                     "on the VPC OR additional subnets in AWS — depends on consumer.")

    aws_inputs_hcl = (
        "  # Translated from GCP google_compute_subnetwork.\n"
        "  # Each entry becomes one aws_subnet attached to the supplied vpc_id.\n"
        f"  subnets = {_render_subnets(subnets)}\n"
        "\n"
        "  # TODO: wire to networking module's vpc_ids output\n"
        '  vpc_id = "vpc-TODO"\n'
    )

    return Translation(
        service_name=SERVICE_NAME,
        aws_inputs_hcl=aws_inputs_hcl,
        notes=notes,
    )


def _render_subnets(subnets: list) -> str:
    if not subnets:
        return "{}"
    lines = ["{"]
    for s in subnets:
        key = s["name"].replace("-", "_").replace(".", "_")
        # Sanitize HCL interpolation tokens in the key for safety
        import re
        key = re.sub(r"\$\{[^}]*\}", "", key).strip("_") or "subnet"
        lines.append(f'    "{key}" = {{')
        lines.append(f'      name                  = "{s["name"]}"')
        lines.append(f'      cidr                  = "{s["cidr"]}"')
        lines.append(f'      private_google_access = {str(s["private_google_access"]).lower()}')
        if s["secondaries"]:
            lines.append("      secondary_cidrs = [")
            for sec in s["secondaries"]:
                lines.append(f'        {{ name = "{sec["name"]}", cidr = "{sec["cidr"]}" }},')
            lines.append("      ]")
        else:
            lines.append("      secondary_cidrs = []")
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


_MAIN_TF = '''# AWS Subnet module — emitted by Cloud Lifecycle Intelligence Migrator.
#
# Companion to the vpc module: this one ATTACHES subnets to an
# existing VPC (passed via var.vpc_id). Use vpc module when you
# also need to create the VPC itself.
#
# Translates the customer's GCP google_compute_subnetwork stacks
# (e.g., "net-subnets" stacks that add subnets to a shared VPC).

resource "aws_subnet" "this" {
  for_each = var.subnets

  vpc_id     = var.vpc_id
  cidr_block = each.value.cidr

  # AWS subnets are AZ-scoped. Round-robin across availability_zones
  # if provided; otherwise leave AWS to default-place (typically AZ-a).
  availability_zone = length(var.availability_zones) > 0 ? var.availability_zones[index(keys(var.subnets), each.key) % length(var.availability_zones)] : null

  # GCP "private Google access" → AWS auto-assigned-public-IP=false
  # (subnet stays private; VPC endpoints handle service access).
  map_public_ip_on_launch = !each.value.private_google_access

  tags = merge(
    var.tags,
    { Name = each.value.name },
  )
}
'''


_VARIABLES_TF = '''variable "vpc_id" {
  type        = string
  description = "VPC ID to attach subnets to (from networking module output)."
}

variable "subnets" {
  type = map(object({
    name                  = string
    cidr                  = string
    private_google_access = bool
    secondary_cidrs = list(object({
      name = string
      cidr = string
    }))
  }))
  description = "Map of subnet key -> spec. Each becomes one aws_subnet."
  default     = {}
}

variable "availability_zones" {
  type        = list(string)
  description = "AZ list to round-robin subnets across. Leave empty for single-AZ deploys."
  default     = []
}

variable "tags" {
  type    = map(string)
  default = {}
}
'''


_OUTPUTS_TF = '''output "subnet_ids" {
  value = { for k, s in aws_subnet.this : k => s.id }
  description = "Map of subnet key -> AWS subnet ID."
}

output "subnet_cidrs" {
  value = { for k, s in aws_subnet.this : k => s.cidr_block }
}

output "subnets_by_az" {
  value = {
    for az in distinct([for s in aws_subnet.this : s.availability_zone if s.availability_zone != null]) :
    az => [for k, s in aws_subnet.this : s.id if s.availability_zone == az]
  }
  description = "Subnet IDs grouped by AZ — useful for placing per-AZ resources."
}
'''


_README = '''# AWS Subnet module

Translates GCP `google_compute_subnetwork` for "subnet-only" stacks —
those that add subnets to an existing shared VPC, not the stack that
creates the VPC itself. (Pair with `vpc` module if you want a VPC
created alongside its subnets.)

## GCP→AWS subnet mapping

| GCP | AWS |
|---|---|
| Subnet (region-scoped) | Subnet (AZ-scoped — typically 1 GCP subnet → 2-3 AWS subnets for HA) |
| `secondary_ip_ranges` (for GKE pods/services) | Additional VPC CIDR blocks OR additional subnets |
| `private_ip_google_access` | `map_public_ip_on_launch = false` + VPC endpoints separately |
| PSC subnets (`subnets_psc`) | No direct analog — translates to PrivateLink/VPC Endpoint at consumer side |

## Required wiring

- `vpc_id` from your networking module output
- `availability_zones` to round-robin subnets across AZs (recommended for HA)
'''
