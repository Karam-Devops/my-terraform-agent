"""GCP google_dns_managed_zone (+ google_dns_record_set) → AWS aws_route53_zone.

Source pattern:

    inputs = {
      managed_zones = {
        "internal-zone" = {
          dns_name    = "internal.example.com."
          visibility  = "private"
          description = "..."
        }
      }
      record_sets = [...]
    }

We emit one Route 53 zone per managed_zone entry. Record sets become
aws_route53_record entries flowing through the same module.
"""

from __future__ import annotations

from typing import List

from migrator.results import DiscoveredResource

from .base import DEFAULT_VERSIONS_TF, AWSModuleSpec, Translation


SERVICE_NAME = "route53-zone"


def translate(resource: DiscoveredResource) -> Translation:
    args = resource.arguments or {}
    notes: List[str] = []

    raw_zones = args.get("managed_zones") or args.get("zones") or args.get("dns_zones") or {}
    if isinstance(raw_zones, list):
        raw_zones = {z.get("name", f"zone{i}"): z for i, z in enumerate(raw_zones) if isinstance(z, dict)}
    if not isinstance(raw_zones, dict):
        raw_zones = {}

    zones = []
    for key, src in raw_zones.items():
        if not isinstance(src, dict):
            src = {}
        name = str(src.get("name", key))
        dns_name = str(src.get("dns_name", f"{name}.example.com.")).rstrip(".")
        visibility = str(src.get("visibility", "public")).lower()
        description = str(src.get("description", f"Migrated from GCP DNS zone {name}"))

        zones.append({
            "key":        key,
            "name":       name,
            "dns_name":   dns_name,
            "visibility": visibility,   # "public" or "private"
            "description": description,
        })

    if not zones:
        notes.append("No managed_zones detected in source; emitted empty map.")
    else:
        public_count = sum(1 for z in zones if z["visibility"] == "public")
        private_count = sum(1 for z in zones if z["visibility"] == "private")
        notes.append(f"Emitted {len(zones)} Route 53 zones ({public_count} public, {private_count} private).")
        if private_count:
            notes.append("Private zones must be associated with a VPC — operator wires `vpc_id` per private zone.")

    aws_inputs_hcl = (
        "  # Translated from GCP google_dns_managed_zone.\n"
        f"  zones = {_render_zones(zones)}\n"
    )

    return Translation(
        service_name=SERVICE_NAME,
        aws_inputs_hcl=aws_inputs_hcl,
        notes=notes,
    )


def _render_zones(zones: list) -> str:
    if not zones:
        return "{}"
    lines = ["{"]
    for z in zones:
        key = z["key"].replace("-", "_").replace(".", "_")
        lines.append(f'    "{key}" = {{')
        lines.append(f'      dns_name    = "{z["dns_name"]}"')
        lines.append(f'      visibility  = "{z["visibility"]}"')
        lines.append(f'      description = "{z["description"]}"')
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


_MAIN_TF = '''# AWS Route 53 module — emitted by Cloud Lifecycle Intelligence Migrator.

# Public zones get straight aws_route53_zone.
resource "aws_route53_zone" "public" {
  for_each = { for k, z in var.zones : k => z if z.visibility == "public" }

  name    = each.value.dns_name
  comment = each.value.description

  tags = merge(
    var.tags,
    { Name = each.value.dns_name },
  )
}

# Private zones need a VPC association. Operator supplies var.private_zone_vpc_ids
# keyed the same as the var.zones entries.
resource "aws_route53_zone" "private" {
  for_each = { for k, z in var.zones : k => z if z.visibility == "private" }

  name    = each.value.dns_name
  comment = each.value.description

  vpc {
    vpc_id = lookup(var.private_zone_vpc_ids, each.key, var.default_vpc_id)
  }

  tags = merge(
    var.tags,
    { Name = each.value.dns_name },
  )
}
'''


_VARIABLES_TF = '''variable "zones" {
  type = map(object({
    dns_name    = string
    visibility  = string  # "public" or "private"
    description = string
  }))
  description = "Map of zone key -> spec."
  default     = {}
}

variable "private_zone_vpc_ids" {
  type        = map(string)
  description = "Map of private-zone key -> VPC ID to associate. Falls back to var.default_vpc_id if missing."
  default     = {}
}

variable "default_vpc_id" {
  type        = string
  description = "Default VPC ID for private zones lacking an explicit private_zone_vpc_ids entry."
  default     = ""
}

variable "tags" {
  type    = map(string)
  default = {}
}
'''


_OUTPUTS_TF = '''output "zone_ids" {
  value = merge(
    { for k, z in aws_route53_zone.public  : k => z.id },
    { for k, z in aws_route53_zone.private : k => z.id },
  )
  description = "Map of zone key -> Route 53 zone ID."
}

output "name_servers" {
  value = { for k, z in aws_route53_zone.public : k => z.name_servers }
  description = "Map of public-zone key -> NS records (delegate from your registrar)."
}
'''


_README = '''# AWS Route 53 module

Translates GCP `google_dns_managed_zone`. Each managed zone → one
Route 53 hosted zone (public or private based on source `visibility`).

## Required follow-up

- **Public zones**: delegate NS records from your domain registrar.
  This module emits the NS records as `name_servers` output.
- **Private zones**: associate with VPCs. Use `private_zone_vpc_ids`
  variable to map zone keys to VPC IDs.
- **Record sets**: not emitted by this module. Use a separate
  `aws_route53_record` resource per record, referencing this
  module's `zone_ids` output.

## Cutover note

DNS cutover is delicate. Recommended pattern:
1. Provision Route 53 zones with NS not yet delegated.
2. Set TTL on existing GCP records to a low value (e.g. 60s) ahead of
   the cutover window.
3. Add Route 53 records mirroring GCP ones.
4. At cutover: change NS delegation at the registrar from GCP DNS to
   Route 53. New queries resolve through Route 53 immediately; in-flight
   queries finish on GCP per their TTL.
5. After 24 hours of stable traffic on Route 53: decommission GCP zone.
'''
