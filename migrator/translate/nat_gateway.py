"""GCP google_compute_router_nat (Cloud NAT) → AWS aws_nat_gateway.

Source pattern (Cloud NAT typically wraps simple egress config):

    inputs = {
      project_id     = ...
      region         = ...
      router_name    = "..."
      nat_configs    = { name = "...", source_subnetwork_ip_ranges_to_nat = "ALL_SUBNETWORKS_ALL_IP_RANGES" }
    }

We emit one NAT Gateway per AZ for HA (recommended AWS pattern).
"""

from __future__ import annotations

from typing import List

from migrator.results import DiscoveredResource

from .base import DEFAULT_VERSIONS_TF, AWSModuleSpec, Translation


SERVICE_NAME = "nat-gateway"


def translate(resource: DiscoveredResource) -> Translation:
    args = resource.arguments or {}
    notes: List[str] = []

    nat_configs = args.get("nat_configs") or args.get("nat_config") or {}
    if not isinstance(nat_configs, dict):
        nat_configs = {"default": {"name": "default-nat"}}

    name = "default-nat"
    if isinstance(nat_configs, dict):
        # Take the first config — Cloud NAT is typically 1-per-router.
        for k, cfg in nat_configs.items():
            if isinstance(cfg, dict) and "name" in cfg:
                name = str(cfg["name"])
                break

    notes.append(
        "GCP Cloud NAT is per-VPC (single resource); AWS NAT Gateway is per-AZ. "
        "Emitting one NAT Gateway per public-subnet AZ for HA — operator must "
        "supply public_subnet_ids list."
    )
    notes.append(
        "Routes: ensure private subnet route tables route 0.0.0.0/0 to the "
        "appropriate NAT GW (same AZ). Module emits the routes when "
        "private_subnet_route_table_ids is provided."
    )

    aws_inputs_hcl = (
        "  # Translated from GCP Cloud NAT (google_compute_router_nat).\n"
        "  # AWS NAT Gateway is per-AZ — one created per public subnet supplied.\n"
        f'  name = "{name}"\n'
        "\n"
        "  # TODO: wire to networking module outputs\n"
        "  public_subnet_ids              = []  # one per AZ for HA\n"
        "  private_subnet_route_table_ids = []  # for the egress 0.0.0.0/0 route\n"
    )

    return Translation(
        service_name=SERVICE_NAME,
        aws_inputs_hcl=aws_inputs_hcl,
        notes=notes,
    )


def aws_module_spec() -> AWSModuleSpec:
    return AWSModuleSpec(
        service_name=SERVICE_NAME,
        main_tf=_MAIN_TF,
        variables_tf=_VARIABLES_TF,
        outputs_tf=_OUTPUTS_TF,
        versions_tf=DEFAULT_VERSIONS_TF,
        readme_md=_README,
    )


_MAIN_TF = '''# AWS NAT Gateway module — emitted by Cloud Lifecycle Intelligence Migrator.
# Translates GCP Cloud NAT (google_compute_router_nat).

resource "aws_eip" "nat" {
  for_each = toset(var.public_subnet_ids)

  domain = "vpc"
  tags = merge(
    var.tags,
    { Name = "${var.name}-eip-${each.key}" },
  )
}

resource "aws_nat_gateway" "this" {
  for_each = toset(var.public_subnet_ids)

  allocation_id = aws_eip.nat[each.key].id
  subnet_id     = each.key

  tags = merge(
    var.tags,
    { Name = "${var.name}-${each.key}" },
  )

  depends_on = [aws_eip.nat]
}

# Optional: add a default route 0.0.0.0/0 to NAT in each private route table.
# This requires that public_subnet_ids and private_subnet_route_table_ids are
# in matching AZ order — operator owns this contract.
resource "aws_route" "private_egress" {
  count = length(var.private_subnet_route_table_ids) > 0 ? min(length(var.public_subnet_ids), length(var.private_subnet_route_table_ids)) : 0

  route_table_id         = var.private_subnet_route_table_ids[count.index]
  destination_cidr_block = "0.0.0.0/0"
  nat_gateway_id         = aws_nat_gateway.this[var.public_subnet_ids[count.index]].id
}
'''


_VARIABLES_TF = '''variable "name" {
  type        = string
  description = "Name prefix for the NAT Gateway resources."
}

variable "public_subnet_ids" {
  type        = list(string)
  description = "Public subnet IDs (one per AZ for HA). Each gets one NAT GW + EIP."
  default     = []
}

variable "private_subnet_route_table_ids" {
  type        = list(string)
  description = "Optional: private subnet route table IDs that should egress via the NAT GWs. Order must match public_subnet_ids."
  default     = []
}

variable "tags" {
  type        = map(string)
  description = "Tags merged onto every NAT GW + EIP."
  default     = {}
}
'''


_OUTPUTS_TF = '''output "nat_gateway_ids" {
  value = { for k, n in aws_nat_gateway.this : k => n.id }
  description = "Map of public-subnet-id -> NAT GW ID."
}

output "nat_eips" {
  value = { for k, e in aws_eip.nat : k => e.public_ip }
  description = "Map of public-subnet-id -> NAT GW Elastic IP."
}
'''


_README = '''# AWS NAT Gateway module

Translates GCP Cloud NAT. Note the topology shift: GCP Cloud NAT is
per-VPC (one resource); AWS NAT Gateway is per-AZ (best-practice: one
per public subnet for HA). This module emits one NAT GW + one EIP per
entry in `public_subnet_ids`.

## Inputs

- `public_subnet_ids` — list of public subnet IDs (typically 2-3 for HA across AZs)
- `private_subnet_route_table_ids` — optional, same-order list to wire egress routes
- `name`, `tags` — naming + tagging
'''
