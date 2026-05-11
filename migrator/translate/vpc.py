"""GCP google_compute_network → AWS aws_vpc + subnets.

Source pattern:

    inputs = {
      vpc_name = "vpc-dev-shared"
      vpcs = {
        "vpc-dev-shared" = {
          name = "vpc-dev-shared"
          subnets = [
            { name, region, ip_cidr_range, secondary_ip_ranges = [...] }
          ]
        }
      }
    }

GCP networks are global with regional subnets. AWS VPCs are regional
with zonal subnets. This module emits one VPC + one subnet per
source subnet (operator may opt to multi-AZ each subnet for HA).
"""

from __future__ import annotations

from typing import List

from migrator.results import DiscoveredResource

from .base import DEFAULT_VERSIONS_TF, AWSModuleSpec, Translation


SERVICE_NAME = "vpc"


def translate(resource: DiscoveredResource) -> Translation:
    args = resource.arguments or {}
    notes: List[str] = []

    raw_vpcs = args.get("vpcs") or args.get("vpc_config") or {}
    if isinstance(raw_vpcs, list):
        raw_vpcs = {v.get("name", f"vpc{i}"): v for i, v in enumerate(raw_vpcs) if isinstance(v, dict)}
    if not isinstance(raw_vpcs, dict):
        raw_vpcs = {}

    # Fall back to single-VPC inputs (vpc_name, subnets) for older shapes.
    if not raw_vpcs and ("vpc_name" in args or "subnets" in args):
        raw_vpcs = {
            args.get("vpc_name", "default"): {
                "name":    args.get("vpc_name", "default"),
                "subnets": args.get("subnets", []),
            },
        }

    vpcs = []
    for key, src in raw_vpcs.items():
        if not isinstance(src, dict):
            continue
        name = str(src.get("name", key))
        subnets_src = src.get("subnets", [])
        if not isinstance(subnets_src, list):
            subnets_src = []

        subnets = []
        for s in subnets_src:
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
            })

        # Try to derive a VPC CIDR from subnets (use the first /16 / /20 found).
        vpc_cidr = "10.0.0.0/16"
        if subnets:
            first_subnet_cidr = subnets[0]["cidr"]
            # Best-effort CIDR widening: keep the first 16 bits as VPC CIDR.
            # Only attempt if the source value LOOKS like a literal CIDR —
            # HCL interpolations like ${local.subnet_cfgs.X.ip_cidr_range}
            # would otherwise produce malformed output (split on '.' eats
            # the ${ open-brace, leaving an unclosed interpolation).
            _looks_literal_cidr = (
                "$" not in first_subnet_cidr
                and "{" not in first_subnet_cidr
                and "/" in first_subnet_cidr
            )
            try:
                if not _looks_literal_cidr:
                    # Source CIDR is an interpolation we can't widen safely.
                    # Fall back to the default vpc_cidr; operator decides.
                    raise ValueError("non-literal CIDR; falling back to default")
                base = first_subnet_cidr.split("/")[0]
                octs = base.split(".")
                if len(octs) == 4:
                    vpc_cidr = f"{octs[0]}.{octs[1]}.0.0/16"
            except (IndexError, AttributeError):
                pass

        vpcs.append({
            "key":     key,
            "name":    name,
            "cidr":    vpc_cidr,
            "subnets": subnets,
        })

    if not vpcs:
        notes.append("No VPC config detected in source; emitted single placeholder VPC.")
        vpcs = [{"key": "default", "name": "default-vpc", "cidr": "10.0.0.0/16", "subnets": []}]
    else:
        notes.append(f"Emitted {len(vpcs)} VPC(s) with {sum(len(v['subnets']) for v in vpcs)} subnet entries.")
        notes.append("AWS subnets are AZ-scoped (not region-scoped like GCP). For HA, each source subnet "
                     "should map to 2-3 AWS subnets across AZs — operator decides per env.")
        notes.append("Secondary IP ranges (GCP) → multiple subnets or VPC additional CIDR blocks (AWS).")

    aws_inputs_hcl = (
        "  # Translated from GCP google_compute_network + subnetworks.\n"
        f"  vpcs = {_render_vpcs(vpcs)}\n"
        "\n"
        "  enable_dns_hostnames = true\n"
        "  enable_dns_support   = true\n"
    )

    return Translation(
        service_name=SERVICE_NAME,
        aws_inputs_hcl=aws_inputs_hcl,
        notes=notes,
    )


def _render_vpcs(vpcs: list) -> str:
    if not vpcs:
        return "{}"
    lines = ["{"]
    for v in vpcs:
        key = v["key"].replace("-", "_").replace(".", "_")
        lines.append(f'    "{key}" = {{')
        lines.append(f'      name = "{v["name"]}"')
        lines.append(f'      cidr = "{v["cidr"]}"')
        if v["subnets"]:
            lines.append("      subnets = [")
            for s in v["subnets"]:
                lines.append("        {")
                lines.append(f'          name = "{s["name"]}"')
                lines.append(f'          cidr = "{s["cidr"]}"')
                lines.append("          # GCP subnets are regional; in AWS each subnet is AZ-scoped.")
                lines.append("          # Operator may want to expand this to multiple AZ subnets for HA.")
                lines.append("        },")
            lines.append("      ]")
        else:
            lines.append("      subnets = []")
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


_MAIN_TF = '''# AWS VPC module — emitted by Cloud Lifecycle Intelligence Migrator.
# Translates GCP google_compute_network + subnetworks.

resource "aws_vpc" "this" {
  for_each = var.vpcs

  cidr_block           = each.value.cidr
  enable_dns_hostnames = var.enable_dns_hostnames
  enable_dns_support   = var.enable_dns_support

  tags = merge(
    var.tags,
    { Name = each.value.name },
  )
}

locals {
  flat_subnets = flatten([
    for vk, v in var.vpcs : [
      for s in v.subnets : {
        vpc_key = vk
        name    = s.name
        cidr    = s.cidr
      }
    ]
  ])
}

resource "aws_subnet" "this" {
  for_each = { for s in local.flat_subnets : "${s.vpc_key}__${s.name}" => s }

  vpc_id     = aws_vpc.this[each.value.vpc_key].id
  cidr_block = each.value.cidr

  # AWS subnets are AZ-scoped — round-robin across availability zones if
  # var.availability_zones is provided.
  availability_zone = length(var.availability_zones) > 0 ? var.availability_zones[index(local.flat_subnets, each.value) % length(var.availability_zones)] : null

  tags = merge(
    var.tags,
    { Name = each.value.name },
  )
}

resource "aws_internet_gateway" "this" {
  for_each = var.vpcs

  vpc_id = aws_vpc.this[each.key].id

  tags = merge(
    var.tags,
    { Name = "${each.value.name}-igw" },
  )
}
'''


_VARIABLES_TF = '''variable "vpcs" {
  type = map(object({
    name = string
    cidr = string
    subnets = list(object({
      name = string
      cidr = string
    }))
  }))
  description = "Map of VPC key -> spec. Each becomes one aws_vpc + N aws_subnet resources."
  default     = {}
}

variable "availability_zones" {
  type        = list(string)
  description = "AZ list to round-robin subnets across. Leave empty for single-AZ deploys."
  default     = []
}

variable "enable_dns_hostnames" {
  type    = bool
  default = true
}

variable "enable_dns_support" {
  type    = bool
  default = true
}

variable "tags" {
  type    = map(string)
  default = {}
}
'''


_OUTPUTS_TF = '''output "vpc_ids" {
  value = { for k, v in aws_vpc.this : k => v.id }
}

output "vpc_cidrs" {
  value = { for k, v in aws_vpc.this : k => v.cidr_block }
}

output "subnet_ids" {
  value = { for k, s in aws_subnet.this : k => s.id }
}

output "internet_gateway_ids" {
  value = { for k, ig in aws_internet_gateway.this : k => ig.id }
}
'''


_README = '''# AWS VPC module

Translates GCP `google_compute_network` + subnetworks. Each VPC →
`aws_vpc` + N `aws_subnet` + 1 internet gateway.

## GCP→AWS topology differences

| GCP | AWS |
|---|---|
| Networks are GLOBAL | VPCs are REGION-scoped |
| Subnets are REGION-scoped | Subnets are AZ-scoped |
| Auto-created routes | Routes need explicit aws_route_table + association |
| Secondary IP ranges (for GKE pods/services) | Multiple subnets or VPC additional CIDR blocks |

## Required follow-up

- For HA: split each source subnet into 2-3 AWS subnets across AZs (use
  `availability_zones` variable to round-robin).
- Route tables: not in this module. Pair with NAT Gateway module for
  egress, and add explicit aws_route_table + aws_route_table_association.
- Network peering: separate `aws_vpc_peering_connection` resource;
  customer's GCP NCC hub-spoke topology likely maps to AWS Transit
  Gateway (different module).
'''
