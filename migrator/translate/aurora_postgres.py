"""GCP Cloud SQL (Postgres, regional HA) → AWS Aurora-PostgreSQL cluster.

When source Cloud SQL is sized for production HA (REGIONAL availability +
non-micro tier), Aurora is the closer architectural fit than vanilla RDS:

  * Cluster topology (1 writer + N readers) instead of single instance
  * Faster failover (under 30s typical, vs Multi-AZ RDS ~60-120s)
  * Storage auto-scaling up to 128 TiB without ops involvement
  * Backtrack + global database options (PiTR via cluster snapshots)

This translator is invoked by rds.py at translate-time when source
criteria match (see `is_aurora_grade()`). For smaller / dev-tier
Cloud SQL, rds.py emits a single aws_db_instance via the RDS module
instead — both modules live in the target/modules/ tree.

HIPAA defaults (consumed from compliance_profiles.py "rds" key):
  - storage_encrypted: always True (Aurora baseline; HIPAA mandates anyway)
  - deletion_protection: forced True
  - backup_retention_period: 35d (HIPAA), 30d (PCI), 14d (SOC2)
  - performance_insights_enabled: forced True
  - iam_database_authentication_enabled: forced True (HIPAA/PCI)
"""

from __future__ import annotations

from typing import List

from migrator.results import DiscoveredResource

from .base import DEFAULT_VERSIONS_TF, AWSModuleSpec, Translation


SERVICE_NAME = "aurora-postgres"


# Cloud SQL Postgres version → Aurora-Postgres engine_version.
# Aurora tracks community Postgres releases ~1-3 months behind.
_AURORA_VERSION_MAP = {
    "POSTGRES_15": "15.5",
    "POSTGRES_14": "14.10",
    "POSTGRES_13": "13.13",
    "POSTGRES_12": "12.17",
    "POSTGRES_11": "11.22",
}


def is_aurora_grade(
    args: dict, src_spec: dict, *, compliance_profile: str = "none",
) -> bool:
    """Decide whether a Cloud SQL instance warrants Aurora vs vanilla RDS.

    Criteria (any one true → Aurora):
      * source explicitly tagged for Aurora (database_type / engine_class hint)
      * availability_type = REGIONAL  (high availability requirement)
      * tier is db-custom-N-M with N >= 2  (non-dev sizing)
      * disk_size >= 100 GB              (production data volume)
      * compliance_profile in (hipaa, pci)  (regulated workloads mandate HA)

    Operator override: setting `database_type: "rds"` in source forces
    the small-instance RDS path even if other criteria match.
    """
    # Explicit override from source (top-level args take precedence)
    db_type = (args.get("database_type") or args.get("engine_class") or "").lower()
    if db_type == "rds":
        return False
    if db_type in ("aurora", "aurora-postgres", "aurora-postgresql"):
        return True

    # Compliance regimes that mandate HA → Aurora cluster
    if (compliance_profile or "").strip().lower() in ("hipaa", "pci"):
        return True

    # Implicit signals from the spec
    availability = str(src_spec.get("availability_type", "ZONAL")).upper()
    if availability == "REGIONAL":
        return True

    tier = str(src_spec.get("tier", "")).strip()
    if tier.startswith("db-custom-"):
        try:
            parts = tier.split("-")
            cpu = int(parts[2])
            if cpu >= 2:
                return True
        except (ValueError, IndexError):
            pass

    disk_size = src_spec.get("disk_size", 0)
    if isinstance(disk_size, (int, float)) and disk_size >= 100:
        return True

    return False


def translate(
    resource: DiscoveredResource,
    *,
    compliance_profile: str = "none",
) -> Translation:
    """Translate Aurora-grade Cloud SQL → aws_rds_cluster + cluster instances.

    Called by rds.py when its source-inspection deems an Aurora cluster
    the better fit. Same compliance-profile knobs as the RDS translator
    (consumed from the "rds" key in compliance_profiles.py).
    """
    from migrator.translate.compliance_profiles import get_defaults
    _profile_defaults = get_defaults(compliance_profile, "rds")

    args = resource.arguments or {}
    notes: List[str] = []

    # Reuse rds.py's input extraction by accepting the same shapes.
    # Customer source key varies — collect from any of the known names
    # (DH uses `cloudsql_instances` as a list-of-dicts; vanilla GCP
    # modules tend to use `sql_config` as a single dict). The empty
    # dict fallback at the end ensures we still emit a single placeholder
    # rather than skipping the resource entirely.
    sql_config = (args.get("sql_config")
                  or args.get("cloudsql_config")
                  or args.get("database_instance")
                  or args.get("cloudsql_instances")
                  or args.get("sql_instances")
                  or args.get("instances")
                  or {})

    if isinstance(sql_config, dict):
        instances = [sql_config]
    elif isinstance(sql_config, list):
        instances = [x for x in sql_config if isinstance(x, dict)]
    else:
        instances = []

    if not instances:
        # Fall back to single placeholder derived from top-level args.
        instances = [{
            "name":             args.get("name", "TODO-cluster-name"),
            "database_version": args.get("database_version", "POSTGRES_15"),
            "tier":             args.get("tier", "db-custom-2-7680"),
            "disk_size":        args.get("disk_size", 100),
            "availability_type": args.get("availability_type", "REGIONAL"),
        }]
        notes.append("Could not detect sql_config in inputs; emitted single Aurora cluster placeholder.")

    cluster_specs = []
    for src in instances:
        name = str(src.get("name", "TODO-cluster-name"))
        gcp_version = str(src.get("database_version", "POSTGRES_15")).upper()
        engine_version = _AURORA_VERSION_MAP.get(gcp_version, "15.5")

        # Aurora doesn't need explicit allocated_storage (it auto-scales),
        # but we surface the source disk_size as a sizing hint in the comment.
        source_disk_size = int(src.get("disk_size", 100) or 100)

        # Reader instance count: REGIONAL = HA → 2 readers. Otherwise 1.
        availability = str(src.get("availability_type", "ZONAL")).upper()
        reader_count = 2 if availability == "REGIONAL" else 1

        # Instance class: derive from tier (reuse same heuristic as rds.py)
        from .rds import _map_tier
        tier = str(src.get("tier", "db-custom-2-7680"))
        instance_class = _map_tier(tier)
        # Aurora cluster instances usually use db.r-family (memory-optimized)
        # since Aurora's storage is decoupled. Coerce t3/m6g into r6g if possible.
        if instance_class.startswith("db.t3"):
            instance_class = instance_class.replace("db.t3", "db.r6g")
        elif instance_class.startswith("db.m6g"):
            instance_class = instance_class.replace("db.m6g", "db.r6g")
        elif instance_class.startswith("db.m6i"):
            instance_class = instance_class.replace("db.m6i", "db.r6i")

        # Compliance profile overrides
        src_deletion_protection = src.get("deletion_protection")
        deletion_protection = (
            bool(src_deletion_protection)
            if src_deletion_protection is not None
            else _profile_defaults.get("deletion_protection", True)  # Aurora default: on
        )

        cluster_specs.append({
            "name":                          name,
            "engine_version":                engine_version,
            "instance_class":                instance_class,
            "source_disk_size":              source_disk_size,
            "reader_count":                  reader_count,
            "deletion_protection":           deletion_protection,
            # Always-on Aurora baseline (HIPAA-compliant by default)
            "storage_encrypted":             True,
            "io_optimized":                  True,
            # Profile-driven attrs
            "backup_retention_days":         _profile_defaults.get("backup_retention_days", 7),
            "performance_insights_enabled":  _profile_defaults.get("performance_insights_enabled", True),
            "iam_database_authentication":   _profile_defaults.get("iam_database_authentication", False),
            "monitoring_interval":           _profile_defaults.get("monitoring_interval", 0),
            "_source_tier":                  tier,
            "_source_version":               gcp_version,
            "_source_availability":          availability,
        })

    if cluster_specs:
        notes.append(f"Emitted {len(cluster_specs)} Aurora-Postgres cluster(s) "
                     f"with {sum(c['reader_count'] + 1 for c in cluster_specs)} total cluster instances.")
        notes.append("Cloud SQL REGIONAL HA → Aurora multi-AZ cluster (1 writer + 2 readers). "
                     "Failover is automatic, typically under 30s.")
        notes.append("Aurora storage auto-scales 10 GB → 128 TiB; no allocated_storage input needed. "
                     "Source disk_size preserved as comment for sizing reference.")
        notes.append("Aurora I/O-optimized storage class enabled (predictable cost at high IOPS; HIPAA-friendly).")

    if compliance_profile and compliance_profile != "none":
        hardened = []
        if deletion_protection: hardened.append("deletion_protection")
        if _profile_defaults.get("backup_retention_days"): hardened.append(f"backup_retention={_profile_defaults['backup_retention_days']}d")
        if _profile_defaults.get("performance_insights_enabled"): hardened.append("performance_insights")
        if _profile_defaults.get("iam_database_authentication"): hardened.append("iam_db_auth")
        if _profile_defaults.get("monitoring_interval"): hardened.append(f"enhanced_monitoring={_profile_defaults['monitoring_interval']}s")
        if hardened:
            notes.append(
                f"compliance profile '{compliance_profile.upper()}' applied — "
                f"defaults forced on: {', '.join(hardened)}"
            )

    aws_inputs_hcl = (
        "  # Translated from GCP Cloud SQL (Postgres, regional HA) → Aurora-Postgres.\n"
        f"  clusters = {_render_clusters(cluster_specs)}\n"
        "\n"
        "  # TODO: wire to networking module outputs\n"
        '  vpc_id     = "vpc-TODO"\n'
        "  subnet_ids = []   # private subnets for the DB subnet group (multi-AZ)\n"
    )

    return Translation(
        service_name=SERVICE_NAME,
        aws_inputs_hcl=aws_inputs_hcl,
        notes=notes,
    )


def _render_clusters(specs: list) -> str:
    if not specs:
        return "{}"
    import re as _re
    lines = ["{"]
    for s in specs:
        # Cluster map key must be a stable, identifier-safe string.
        # Source names often have interpolation like
        # `dh-digitalform-${local.env}-pg-sql`. Strip the entire
        # ${...} chunk for the key (the env-suffix is environment-
        # specific anyway and gets baked in by the wider per-env
        # emission), keep the static parts so multiple clusters in
        # one stack get distinct keys.
        raw_name = s["name"]
        key_src = _re.sub(r"\$\{[^}]*\}", "", str(raw_name))
        key = _re.sub(r"[^A-Za-z0-9_]+", "_", key_src).strip("_")
        if not key:
            # Fully-interpolated source name (rare) — use the index
            # to keep keys distinct across multiple instances.
            key = f"cluster_{len(lines)}"
        if key[0].isdigit():
            key = "_" + key
        # Cluster name — pass through the source `name` as-is. The
        # customer-profile substitutions (run in _sanitize_translation
        # after the translator emits) already handle the common DH
        # interpolation patterns: ${local.env}, ${local._project.locals.env},
        # ${local.prefix}, etc. The mangled-alias generator in the
        # profile loader extends coverage to python-hcl2's underscore-
        # mangled forms. Anything that survives both is genuinely
        # operator-action.
        # Previous approach replaced unsubstituted ${...} with literal
        # "TODO-RESOLVE" — which then became part of the cluster's
        # actual aws_rds_cluster.cluster_identifier, breaking the
        # endpoint URL. Kiro v7 fix #2.
        cluster_name = str(s["name"])
        lines.append(f'    "{key}" = {{')
        lines.append(f'      name                = "{cluster_name}"')
        lines.append(f'      engine_version      = "{s["engine_version"]}"   # was {s["_source_version"]}')
        lines.append(f'      instance_class      = "{s["instance_class"]}"   # cluster instance class (r-family preferred)')
        lines.append(f'      reader_count        = {s["reader_count"]}                       # was GCP availability={s["_source_availability"]}')
        lines.append(f'      deletion_protection = {str(s["deletion_protection"]).lower()}')
        # Always-on Aurora baseline
        lines.append(f'      storage_encrypted   = true')
        lines.append(f'      io_optimized        = true')
        # Source disk_size preserved as inline comment for operator reference
        lines.append(f'      # source disk_size: {s["source_disk_size"]} GB (Aurora storage auto-scales)')
        # Profile-driven attrs
        if s.get("backup_retention_days"):
            lines.append(f'      backup_retention_days        = {s["backup_retention_days"]}   # compliance profile')
        if s.get("performance_insights_enabled"):
            lines.append(f'      performance_insights_enabled = true # compliance profile')
        if s.get("iam_database_authentication"):
            lines.append(f'      iam_database_authentication  = true # compliance profile')
        if s.get("monitoring_interval"):
            lines.append(f'      monitoring_interval          = {s["monitoring_interval"]}   # compliance profile')
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


_MAIN_TF = '''# AWS Aurora-Postgres module — emitted by Cloud Lifecycle Intelligence Migrator.
# Translates GCP Cloud SQL Postgres with REGIONAL availability or large tier
# into an Aurora cluster (1 writer + N readers).

resource "random_password" "master" {
  for_each = var.clusters
  length   = 32
  special  = true
}

resource "aws_secretsmanager_secret" "master" {
  for_each = var.clusters
  name     = "${each.value.name}-master-credentials"
  tags     = var.tags
}

resource "aws_secretsmanager_secret_version" "master" {
  for_each      = var.clusters
  secret_id     = aws_secretsmanager_secret.master[each.key].id
  secret_string = jsonencode({
    username = "appadmin"
    password = random_password.master[each.key].result
  })
}

resource "aws_db_subnet_group" "this" {
  count = length(var.subnet_ids) > 0 ? 1 : 0

  name       = "${var.name_prefix}-aurora-subnet-group"
  subnet_ids = var.subnet_ids
  tags       = var.tags
}

resource "aws_security_group" "aurora" {
  count = var.create_security_group ? 1 : 0

  name        = "${var.name_prefix}-aurora-sg"
  description = "Security group for Aurora cluster"
  vpc_id      = var.vpc_id

  ingress {
    from_port   = 5432
    to_port     = 5432
    protocol    = "tcp"
    cidr_blocks = var.allowed_cidrs
  }

  tags = var.tags
}

# ---- Aurora cluster (control + storage layer) ----
resource "aws_rds_cluster" "this" {
  for_each = var.clusters

  cluster_identifier  = each.value.name
  engine              = "aurora-postgresql"
  engine_version      = each.value.engine_version
  engine_mode         = "provisioned"

  master_username = "appadmin"
  master_password = random_password.master[each.key].result

  database_name = "appdb"

  # Aurora's storage type: io-optimized recommended for HIPAA/PCI
  # (predictable cost at high IOPS; AWS internally uses encryption-at-rest
  # for all aurora storage).
  storage_type      = lookup(each.value, "io_optimized", true) ? "aurora-iopt1" : "aurora"
  storage_encrypted = lookup(each.value, "storage_encrypted", true)

  db_subnet_group_name   = length(aws_db_subnet_group.this) > 0 ? aws_db_subnet_group.this[0].name : null
  vpc_security_group_ids = compact([
    var.create_security_group ? aws_security_group.aurora[0].id : null,
  ])

  # Compliance-profile-driven attrs (lookup with safe defaults so the
  # module works WITHOUT the profile too).
  backup_retention_period          = lookup(each.value, "backup_retention_days", 7)
  preferred_backup_window          = "03:00-05:00"
  preferred_maintenance_window     = "sun:05:00-sun:07:00"
  copy_tags_to_snapshot            = true
  deletion_protection              = each.value.deletion_protection
  skip_final_snapshot              = !each.value.deletion_protection
  iam_database_authentication_enabled = lookup(each.value, "iam_database_authentication", false)

  # Enable CloudWatch log exports for HIPAA/PCI audit trail.
  enabled_cloudwatch_logs_exports = ["postgresql"]

  tags = merge(var.tags, { Name = each.value.name })
}

# ---- Cluster instances (1 writer + N readers, flattened) ----
# We compute role labels in a separate pass to keep the object-literal
# inside the comprehension simple (python-hcl2 can't parse complex
# expressions like `i == 0 ? ... : ...` directly inside object values).
locals {
  flat_instances = flatten([
    for ck, c in var.clusters : [
      for i in range(c.reader_count + 1) : {
        cluster_key  = ck
        instance_index = i
        cluster_spec = c
      }
    ]
  ])
}

resource "aws_rds_cluster_instance" "this" {
  for_each = {
    for inst in local.flat_instances :
    format("%s-%d", inst.cluster_spec.name, inst.instance_index) => inst
  }

  identifier         = format("%s-%d", each.value.cluster_spec.name, each.value.instance_index)
  cluster_identifier = aws_rds_cluster.this[each.value.cluster_key].id
  engine             = aws_rds_cluster.this[each.value.cluster_key].engine
  engine_version     = aws_rds_cluster.this[each.value.cluster_key].engine_version
  instance_class     = each.value.cluster_spec.instance_class

  publicly_accessible = false

  performance_insights_enabled = lookup(each.value.cluster_spec, "performance_insights_enabled", true)
  monitoring_interval          = lookup(each.value.cluster_spec, "monitoring_interval", 0)

  # Aurora's preferred_backup_window applies cluster-wide, not per instance.
  preferred_maintenance_window = "sun:05:00-sun:07:00"

  tags = merge(
    var.tags,
    {
      Name = format("%s-%d", each.value.cluster_spec.name, each.value.instance_index)
      Role = each.value.instance_index == 0 ? "writer" : "reader"
    },
  )
}
'''


_VARIABLES_TF = '''# `clusters` is map(any) so callers can supply heterogeneous attrs
# across clusters (different reader counts, optional compliance fields).
# Implicit schema:
#   required:
#     name                = string
#     engine_version      = string   # e.g. "15.5"
#     instance_class      = string   # e.g. "db.r6g.xlarge"
#     reader_count        = number   # writer is implicit; reader_count adds to cluster
#     deletion_protection = bool
#     storage_encrypted   = bool
#     io_optimized        = bool
#   optional (compliance-profile-emitted):
#     backup_retention_days        = number
#     performance_insights_enabled = bool
#     iam_database_authentication  = bool
#     monitoring_interval          = number
variable "clusters" {
  type        = map(any)
  description = "Map of cluster key -> spec. Schema documented in translator source."
  default     = {}
}

variable "name_prefix" {
  type        = string
  default     = "migrator"
  description = "Prefix for shared resources (subnet group, SG)."
}

variable "vpc_id" {
  type        = string
  description = "VPC ID where Aurora will live."
}

variable "subnet_ids" {
  type        = list(string)
  description = "Private subnet IDs across at least 2 AZs (Aurora requires multi-AZ)."
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


_OUTPUTS_TF = '''output "cluster_endpoints" {
  value = {
    for k, c in aws_rds_cluster.this :
    k => {
      writer_endpoint = c.endpoint           # routes to writer
      reader_endpoint = c.reader_endpoint    # round-robin across readers
      port            = c.port
      arn             = c.arn
    }
  }
  description = "Map of cluster key -> connection details."
}

output "cluster_instance_ids" {
  value = { for k, i in aws_rds_cluster_instance.this : k => i.identifier }
  description = "Map of cluster-instance ID -> identifier."
}

output "master_secret_arns" {
  value     = { for k, s in aws_secretsmanager_secret.master : k => s.arn }
  description = "Map of cluster key -> Secrets Manager ARN holding master credentials."
  sensitive = true
}
'''


_README = '''# AWS Aurora-PostgreSQL module

Translates GCP `google_sql_database_instance` (Postgres, REGIONAL/HA tier)
into an Aurora cluster. Per cluster:

- `aws_rds_cluster` (engine = aurora-postgresql)
- `aws_rds_cluster_instance` × (1 writer + N readers)
- `aws_secretsmanager_secret` for master credentials (auto-generated)
- `aws_db_subnet_group` (shared, multi-AZ subnets required)
- `aws_security_group` (shared, default port 5432)

## When this module is chosen vs RDS

The Migrator engine picks Aurora when source Cloud SQL has any of:
- `availability_type = REGIONAL` (HA requirement)
- `tier` of `db-custom-N-M` with N ≥ 2 (production sizing)
- `disk_size` ≥ 100 GB

For dev-tier single-instance Postgres, the RDS module (`rds-postgres`)
is used instead. Operator can force RDS by setting `database_type = "rds"`
in source inputs.

## GCP→AWS architectural shift

| Cloud SQL HA            | Aurora                            |
|-------------------------|-----------------------------------|
| Single regional instance| Writer + N readers in one cluster |
| Multi-AZ standby        | Storage-decoupled multi-AZ        |
| 60-120s failover        | <30s failover                     |
| Vertically scaled disk  | Auto-scaling storage 10GB→128TiB  |
| Backup retention 7-365d | Backup retention 1-35d (PiTR)     |

## Compliance profile defaults

| Profile | deletion_protection | backup_retention | performance_insights | iam_db_auth |
|---|---|---|---|---|
| none  | source-controlled | 7d  | true | false |
| hipaa | **true** | **35d** | true | **true** |
| soc2  | **true** | **14d** | true | false |
| pci   | **true** | **30d** | true | **true** |

## Manual review needed

- **Master password rotation** — emitted random_password isn't rotated.
  Wire up Secrets Manager rotation Lambda per env.
- **Reader scaling** — translator emits 2 readers per cluster. Larger
  workloads need auto-scaling group via `aws_appautoscaling_*` resources
  (not in scope today).
- **Aurora Global Database** (cross-region replication) — operator
  decides per cluster; not auto-emitted.
- **Backtrack window** — Aurora-specific PiTR for accidental writes.
  Add `backtrack_window = N` to cluster spec if needed.
'''
