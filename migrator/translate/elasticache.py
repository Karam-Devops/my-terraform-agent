"""GCP google_redis_instance (Memorystore) → AWS aws_elasticache_replication_group.

Source pattern:

    inputs = {
      network        = "projects/<proj>/global/networks/vpc-..."
      primary_zone   = "northamerica-northeast1-a"
      primary_region = "northamerica-northeast1"
      dxw_redis_instances = [
        { name = "falconops-redis" },
        ...
      ]
    }

We treat each entry as one ElastiCache Redis replication group. Memory
sizes, node types, and security groups need operator review — the
source rarely specifies them at this level.
"""

from __future__ import annotations

from typing import List

from migrator.results import DiscoveredResource

from .base import DEFAULT_VERSIONS_TF, AWSModuleSpec, Translation


SERVICE_NAME = "elasticache-redis"


def translate(resource: DiscoveredResource) -> Translation:
    args = resource.arguments or {}
    notes: List[str] = []

    raw_instances = (
        args.get("dxw_redis_instances")
        or args.get("redis_instances")
        or args.get("instances")
        or []
    )
    if not isinstance(raw_instances, list):
        raw_instances = []

    instances = []
    for src in raw_instances:
        if isinstance(src, dict):
            name = str(src.get("name", "TODO-redis-name"))
        elif isinstance(src, str):
            name = src
        else:
            continue
        instances.append({
            "name":              name,
            "node_type":         "cache.t3.micro",   # default — operator overrides
            "engine_version":    "7.0",
            "parameter_group":   "default.redis7",
            "automatic_failover": False,
            "num_node_groups":   1,
            "replicas_per_node": 0,
        })

    if not instances:
        notes.append("No redis_instances found in source; emitted empty map.")
    else:
        notes.append(f"Emitted {len(instances)} ElastiCache Redis entr{'y' if len(instances)==1 else 'ies'}. "
                     "Default node_type=cache.t3.micro — review per-instance sizing against GCP Memorystore tier.")
        notes.append("VPC + subnet + SG references are TODO — wire to your AWS networking module outputs.")

    aws_inputs_hcl = (
        "  # Translated from GCP dxw_redis_instances list.\n"
        "  # Each entry becomes one ElastiCache Redis replication group.\n"
        f"  redis_instances = {_render_instances(instances)}\n"
        "\n"
        "  # TODO: wire to networking module outputs\n"
        '  vpc_id     = "vpc-TODO"\n'
        "  subnet_ids = []  # populate from networking module\n"
    )

    return Translation(
        service_name=SERVICE_NAME,
        aws_inputs_hcl=aws_inputs_hcl,
        notes=notes,
    )


def _render_instances(instances: list) -> str:
    if not instances:
        return "{}"
    lines = ["{"]
    for i in instances:
        key = i["name"].replace("-", "_").replace(".", "_")
        lines.append(f'    "{key}" = {{')
        lines.append(f'      name                = "{i["name"]}"')
        lines.append(f'      node_type           = "{i["node_type"]}"')
        lines.append(f'      engine_version      = "{i["engine_version"]}"')
        lines.append(f'      parameter_group     = "{i["parameter_group"]}"')
        lines.append(f'      automatic_failover  = {str(i["automatic_failover"]).lower()}')
        lines.append(f'      num_node_groups     = {i["num_node_groups"]}')
        lines.append(f'      replicas_per_node   = {i["replicas_per_node"]}')
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


_MAIN_TF = '''# AWS ElastiCache Redis module — emitted by Cloud Lifecycle Intelligence Migrator.
# Translates GCP google_redis_instance (Memorystore Redis) to AWS
# ElastiCache Redis replication groups.
#
# Swap path: replace this main.tf only. variables.tf + outputs.tf are
# the swap interface — keep stable.

resource "aws_elasticache_subnet_group" "this" {
  count = length(var.subnet_ids) > 0 ? 1 : 0

  name       = "${var.name_prefix}-subnet-group"
  subnet_ids = var.subnet_ids

  tags = var.tags
}

resource "aws_security_group" "this" {
  count = var.create_security_group ? 1 : 0

  name        = "${var.name_prefix}-redis-sg"
  description = "Security group for ElastiCache Redis"
  vpc_id      = var.vpc_id

  ingress {
    from_port       = 6379
    to_port         = 6379
    protocol        = "tcp"
    cidr_blocks     = var.allowed_cidrs
  }

  tags = var.tags
}

resource "aws_elasticache_replication_group" "this" {
  for_each = var.redis_instances

  replication_group_id = each.value.name
  description          = "Redis replication group for ${each.value.name}"

  node_type            = each.value.node_type
  engine_version       = each.value.engine_version
  parameter_group_name = each.value.parameter_group
  port                 = 6379

  automatic_failover_enabled = each.value.automatic_failover
  num_node_groups            = each.value.num_node_groups
  replicas_per_node_group    = each.value.replicas_per_node

  subnet_group_name = length(aws_elasticache_subnet_group.this) > 0 ? aws_elasticache_subnet_group.this[0].name : null
  security_group_ids = compact([
    var.create_security_group ? aws_security_group.this[0].id : null,
  ])

  at_rest_encryption_enabled = true
  transit_encryption_enabled = true

  tags = merge(
    var.tags,
    { Name = each.value.name },
  )
}
'''


_VARIABLES_TF = '''variable "redis_instances" {
  type = map(object({
    name                = string
    node_type           = string
    engine_version      = string
    parameter_group     = string
    automatic_failover  = bool
    num_node_groups     = number
    replicas_per_node   = number
  }))
  description = "Map of redis instance key -> spec. Each becomes one aws_elasticache_replication_group."
  default     = {}
}

variable "name_prefix" {
  type        = string
  description = "Prefix for subnet group + SG names."
  default     = "migrator"
}

variable "vpc_id" {
  type        = string
  description = "VPC ID where ElastiCache will live."
}

variable "subnet_ids" {
  type        = list(string)
  description = "Subnet IDs for the ElastiCache subnet group (typically private subnets, multi-AZ)."
  default     = []
}

variable "create_security_group" {
  type        = bool
  description = "Whether to create a default Redis SG (port 6379)."
  default     = true
}

variable "allowed_cidrs" {
  type        = list(string)
  description = "CIDRs that can reach Redis (default: same VPC)."
  default     = []
}

variable "tags" {
  type        = map(string)
  description = "Tags merged onto every resource."
  default     = {}
}
'''


_OUTPUTS_TF = '''output "redis_endpoints" {
  value = {
    for k, r in aws_elasticache_replication_group.this :
    k => {
      primary_endpoint = r.primary_endpoint_address
      reader_endpoint  = r.reader_endpoint_address
      port             = r.port
      arn              = r.arn
    }
  }
  description = "Map of redis instance key -> endpoint config."
}
'''


_README = '''# AWS ElastiCache Redis module

Translates GCP `google_redis_instance` (Memorystore Redis). Each entry
becomes one ElastiCache Redis replication group with default
encryption-at-rest + in-transit enabled.

## Inputs you'll need to provide

- `vpc_id` — your AWS VPC ID
- `subnet_ids` — at least 2 private subnets across AZs for HA
- `redis_instances` — map of name → spec (node_type, engine_version, etc.)

## Notes

- Memorystore tier translation: BASIC → no failover; STANDARD_HA → automatic_failover=true.
- Default node_type=cache.t3.micro; tune per-instance based on Memorystore size_gb.
- Encryption-at-rest + in-transit ON by default. Override in main.tf if you need it off.
'''
