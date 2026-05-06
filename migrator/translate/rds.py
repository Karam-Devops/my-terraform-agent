"""GCP google_sql_database_instance (Cloud SQL) → AWS aws_db_instance.

Cloud SQL inputs vary by version (POSTGRES_15, POSTGRES_13, MYSQL_8) and
by tier convention (db-custom-N-M, db-f1-micro). We map the most common
fields and surface anything ambiguous as a TODO.
"""

from __future__ import annotations

import re
from typing import List

from migrator.results import DiscoveredResource

from .base import DEFAULT_VERSIONS_TF, AWSModuleSpec, Translation


SERVICE_NAME = "rds-postgres"


# Cloud SQL DB version → RDS engine + version.
_VERSION_MAP = {
    "POSTGRES_15": ("postgres", "15"),
    "POSTGRES_14": ("postgres", "14"),
    "POSTGRES_13": ("postgres", "13"),
    "POSTGRES_12": ("postgres", "12"),
    "MYSQL_8_0":   ("mysql",    "8.0"),
    "MYSQL_5_7":   ("mysql",    "5.7"),
}


# Custom tier (db-custom-N-M) where N=cpu, M=memory_mb → RDS instance class.
def _custom_tier_to_instance_class(cpu: int, memory_mb: int) -> str:
    """Best-effort match db-custom-N-M to an AWS RDS instance class."""
    # Memory ratio guides class family choice.
    mem_per_cpu = memory_mb / cpu if cpu > 0 else 0
    # General-purpose default
    if mem_per_cpu >= 7000:
        # Memory-optimized
        family = "r6g"
    elif mem_per_cpu >= 3500:
        family = "m6g"
    else:
        family = "t3"
    if cpu <= 1:
        return f"db.{family}.micro" if family == "t3" else f"db.{family}.large"
    if cpu <= 2:
        return f"db.{family}.medium" if family == "t3" else f"db.{family}.large"
    if cpu <= 4:
        return f"db.{family}.large" if family == "t3" else f"db.{family}.xlarge"
    if cpu <= 8:
        return f"db.{family}.xlarge"
    if cpu <= 16:
        return f"db.{family}.2xlarge"
    return f"db.{family}.4xlarge"


def _map_tier(tier: str) -> str:
    """Map a Cloud SQL tier string to an RDS instance class."""
    tier = (tier or "").strip()
    if tier == "db-f1-micro":
        return "db.t3.micro"
    if tier == "db-g1-small":
        return "db.t3.small"
    m = re.match(r"db-custom-(\d+)-(\d+)", tier)
    if m:
        return _custom_tier_to_instance_class(int(m.group(1)), int(m.group(2)))
    # Unknown — default to t3.medium with a TODO comment in the output
    return "db.t3.medium"


def translate(resource: DiscoveredResource) -> Translation:
    args = resource.arguments or {}
    notes: List[str] = []

    # The customer's input shape varies. Try the common keys.
    sql_config = (args.get("sql_config")
                  or args.get("cloudsql_config")
                  or args.get("database_instance")
                  or {})

    if isinstance(sql_config, dict):
        instances = [sql_config]
    elif isinstance(sql_config, list):
        instances = [x for x in sql_config if isinstance(x, dict)]
    else:
        instances = []

    if not instances:
        # Fall back: synthesize a single placeholder from top-level args.
        instances = [{
            "name":             args.get("name", "TODO-db-name"),
            "database_version": args.get("database_version", "POSTGRES_15"),
            "tier":             args.get("tier", "db-f1-micro"),
            "disk_size":        args.get("disk_size", 20),
            "availability_type": args.get("availability_type", "ZONAL"),
        }]
        notes.append("Could not detect sql_config / cloudsql_config in inputs; "
                     "emitted single placeholder DB instance — review.")

    rds_specs = []
    for src in instances:
        name = str(src.get("name", "TODO-db-name"))
        gcp_version = str(src.get("database_version", "POSTGRES_15")).upper()
        engine, engine_version = _VERSION_MAP.get(gcp_version, ("postgres", "15"))

        tier = str(src.get("tier", "db-f1-micro"))
        instance_class = _map_tier(tier)

        availability = str(src.get("availability_type", "ZONAL")).upper()
        multi_az = (availability == "REGIONAL")

        rds_specs.append({
            "name":             name,
            "engine":           engine,
            "engine_version":   engine_version,
            "instance_class":   instance_class,
            "allocated_storage": int(src.get("disk_size", 20) or 20),
            "multi_az":         multi_az,
            "deletion_protection": bool(src.get("deletion_protection", False)),
            "_source_tier":     tier,
            "_source_version":  gcp_version,
        })

    if rds_specs:
        notes.append(f"Emitted {len(rds_specs)} RDS instance entr{'y' if len(rds_specs)==1 else 'ies'}.")
        notes.append("Cloud SQL HA (REGIONAL) → RDS Multi-AZ. Different replication semantics; review SLA.")
        notes.append("PSA private IP → RDS in same VPC subnet group (no peering needed in AWS).")
    notes.append("Master password generation: use AWS Secrets Manager, not inline. "
                 "module emits a random_password resource by default.")

    aws_inputs_hcl = (
        "  # Translated from GCP Cloud SQL.\n"
        f"  databases = {_render_databases(rds_specs)}\n"
        "\n"
        "  # TODO: wire to networking module outputs\n"
        '  vpc_id     = "vpc-TODO"\n'
        "  subnet_ids = []  # private subnets for the DB subnet group\n"
    )

    return Translation(
        service_name=SERVICE_NAME,
        aws_inputs_hcl=aws_inputs_hcl,
        notes=notes,
    )


def _render_databases(specs: list) -> str:
    if not specs:
        return "{}"
    lines = ["{"]
    for s in specs:
        key = s["name"].replace("-", "_").replace(".", "_")
        lines.append(f'    "{key}" = {{')
        lines.append(f'      name              = "{s["name"]}"')
        lines.append(f'      engine            = "{s["engine"]}"')
        lines.append(f'      engine_version    = "{s["engine_version"]}"')
        lines.append(f'      instance_class    = "{s["instance_class"]}"     # GCP tier {s["_source_tier"]}')
        lines.append(f'      allocated_storage = {s["allocated_storage"]}')
        lines.append(f'      multi_az          = {str(s["multi_az"]).lower()}')
        lines.append(f'      deletion_protection = {str(s["deletion_protection"]).lower()}')
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


_MAIN_TF = '''# AWS RDS module — emitted by Cloud Lifecycle Intelligence Migrator.
# Translates GCP Cloud SQL (google_sql_database_instance) for both
# Postgres and MySQL engines.

resource "random_password" "master" {
  for_each = var.databases
  length   = 32
  special  = true
}

resource "aws_secretsmanager_secret" "master" {
  for_each = var.databases
  name     = "${each.value.name}-master-credentials"
  tags     = var.tags
}

resource "aws_secretsmanager_secret_version" "master" {
  for_each      = var.databases
  secret_id     = aws_secretsmanager_secret.master[each.key].id
  secret_string = jsonencode({
    username = "appadmin"
    password = random_password.master[each.key].result
  })
}

resource "aws_db_subnet_group" "this" {
  count = length(var.subnet_ids) > 0 ? 1 : 0

  name       = "${var.name_prefix}-db-subnet-group"
  subnet_ids = var.subnet_ids
  tags       = var.tags
}

resource "aws_security_group" "db" {
  count = var.create_security_group ? 1 : 0

  name        = "${var.name_prefix}-db-sg"
  description = "Security group for RDS"
  vpc_id      = var.vpc_id

  ingress {
    from_port   = 5432
    to_port     = 5432
    protocol    = "tcp"
    cidr_blocks = var.allowed_cidrs
  }

  tags = var.tags
}

resource "aws_db_instance" "this" {
  for_each = var.databases

  identifier        = each.value.name
  engine            = each.value.engine
  engine_version    = each.value.engine_version
  instance_class    = each.value.instance_class
  allocated_storage = each.value.allocated_storage
  storage_type      = "gp3"
  storage_encrypted = true

  username = "appadmin"
  password = random_password.master[each.key].result

  multi_az            = each.value.multi_az
  deletion_protection = each.value.deletion_protection
  skip_final_snapshot = !each.value.deletion_protection

  db_subnet_group_name = length(aws_db_subnet_group.this) > 0 ? aws_db_subnet_group.this[0].name : null
  vpc_security_group_ids = compact([
    var.create_security_group ? aws_security_group.db[0].id : null,
  ])

  backup_retention_period = each.value.deletion_protection ? 14 : 1
  performance_insights_enabled = true

  tags = merge(var.tags, { Name = each.value.name })
}
'''


_VARIABLES_TF = '''variable "databases" {
  type = map(object({
    name                = string
    engine              = string  # postgres | mysql
    engine_version      = string
    instance_class      = string  # e.g. db.t3.medium, db.r6g.xlarge
    allocated_storage   = number
    multi_az            = bool
    deletion_protection = bool
  }))
  description = "Map of DB key -> spec. Each entry creates one aws_db_instance + secrets."
  default     = {}
}

variable "name_prefix" {
  type        = string
  default     = "migrator"
  description = "Prefix for shared resources (subnet group, SG)."
}

variable "vpc_id" {
  type        = string
  description = "VPC ID where RDS will live."
}

variable "subnet_ids" {
  type        = list(string)
  description = "Private subnet IDs (multi-AZ recommended)."
  default     = []
}

variable "create_security_group" {
  type    = bool
  default = true
}

variable "allowed_cidrs" {
  type    = list(string)
  default = []
}

variable "tags" {
  type    = map(string)
  default = {}
}
'''


_OUTPUTS_TF = '''output "db_endpoints" {
  value = {
    for k, db in aws_db_instance.this :
    k => {
      address = db.address
      port    = db.port
      arn     = db.arn
    }
  }
  description = "Map of DB key -> connection details."
}

output "secret_arns" {
  value = {
    for k, s in aws_secretsmanager_secret.master :
    k => s.arn
  }
  description = "Map of DB key -> Secrets Manager ARN holding master credentials."
  sensitive   = true
}
'''


_README = '''# AWS RDS module

Translates GCP `google_sql_database_instance` (Cloud SQL Postgres / MySQL).
Each DB gets:
- `aws_db_instance`
- `aws_secretsmanager_secret` for master credentials (auto-generated password)
- `aws_db_subnet_group` (shared)
- `aws_security_group` (shared, default port 5432)

## GCP→AWS mapping notes

- `database_version: POSTGRES_15` → `engine=postgres engine_version=15`
- `tier: db-custom-N-M` → instance class chosen via memory-per-CPU heuristic (review for tier match)
- `availability_type: REGIONAL` → `multi_az=true`
- `disk_size` → `allocated_storage`
- PSA private IP → automatic via `db_subnet_group_name` (no separate peering)

## Manual review needed

- Master password: auto-generated and stored in Secrets Manager. To migrate
  existing GCP user data: dump from Cloud SQL, restore to RDS via DMS or pg_restore.
- Maintenance window: not translated — set per-customer policy.
- Read replicas: not in scope; add separately if needed.
'''
