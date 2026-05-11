"""Migrator translate layer — Design phase per-type translators.

Each translator handles one GCP resource type and produces:
  * `Translation` — the AWS-equivalent inputs to embed in the leaf
    `terragrunt.hcl`'s `inputs = { ... }` block.
  * `AWSModuleSpec` — the AWS module body files (main.tf + variables.tf
    + outputs.tf + versions.tf + README.md) emitted under
    `target/modules/<service>/`.

Architecture goals:
  1. **Clear input contract per service**: each AWS module has a
     stable `variables.tf` that customer module bodies (when supplied
     later) must conform to. Swap path: replace `main.tf` only.
  2. **Per-service file co-location**: one Python module per AWS
     service, so engineers can extend coverage by adding a new file
     and registering it in TRANSLATORS.
  3. **Pure functions, no side effects**: translators don't write
     files; the terragrunt_emitter handles I/O. Translators only
     compute strings + dicts. Easier to unit-test.

Currently registered (Tier B scope, demo 2026-05-07):
  google_storage_bucket          → aws_s3_bucket            (gcs_to_s3)
  google_compute_address         → aws_eip                  (eip)
  google_compute_global_address  → aws_eip                  (eip)
  google_redis_instance          → aws_elasticache_*        (elasticache)
  google_compute_router_nat      → aws_nat_gateway          (nat_gateway)
  google_sql_database_instance   → aws_db_instance          (rds)
  google_compute_instance        → aws_instance             (ec2)
  google_pubsub_topic            → aws_sns_topic + aws_sqs  (sns_sqs)
  google_pubsub_subscription     → aws_sqs_queue            (sns_sqs)
"""

from __future__ import annotations

from typing import Dict, List, Optional

from migrator.results import DiscoveredResource

from . import (
    acm,
    ec2,
    ecr,
    eip,
    elasticache,
    eventbridge_scheduler,
    gcs_to_s3,
    log_sink,
    nat_gateway,
    rds,
    route53,
    secrets,
    security_group,
    sns_sqs,
    subnet,
    vpc,
    waf,
)
from .base import AWSModuleSpec, Translation


# GCP tf_type → translator module
TRANSLATORS = {
    # Tier B (initial 7)
    "google_storage_bucket":         gcs_to_s3,
    "google_compute_address":        eip,
    "google_compute_global_address": eip,
    "google_redis_instance":         elasticache,
    "google_compute_router_nat":     nat_gateway,
    "google_sql_database_instance":  rds,
    "google_compute_instance":       ec2,
    "google_pubsub_topic":           sns_sqs,
    "google_pubsub_subscription":    sns_sqs,
    # Tier 1 expansion (added 2026-05-07)
    "google_secret_manager_secret":              secrets,
    "google_artifact_registry_repository":       ecr,
    "google_certificate_manager_certificate":    acm,
    "google_dns_managed_zone":                   route53,
    "google_compute_network":                    vpc,
    "google_compute_subnetwork":                 subnet,
    "google_compute_firewall":                   security_group,
    # Tier 2 expansion (added 2026-05-07)
    "google_compute_security_policy":            waf,
    "google_logging_project_sink":               log_sink,
    "google_cloud_scheduler_job":                eventbridge_scheduler,
}


def translate_resource(
    resource: DiscoveredResource,
    *,
    compliance_profile: str = "none",
) -> Optional[Translation]:
    """Run the per-type translator for a discovered resource.

    Args:
        resource: source GCP resource.
        compliance_profile: one of "none", "hipaa", "soc2", "pci".
            Translators that have opted in apply hardened defaults per
            this profile. Translators that haven't been wired yet just
            ignore it and produce neutral defaults — both code paths
            work side-by-side during incremental rollout.

    Returns None when the resource's tf_type is not yet covered by
    a translator. Caller falls back to the scaffold-only path.
    """
    mod = TRANSLATORS.get(resource.tf_type)
    if mod is None:
        return None
    try:
        # Try the new signature first; fall back to legacy signature
        # for translators not yet migrated. TypeError is the canonical
        # Python signal "this function doesn't accept that kwarg".
        try:
            return mod.translate(resource, compliance_profile=compliance_profile)
        except TypeError:
            return mod.translate(resource)
    except Exception as e:  # noqa: BLE001 — best-effort per-resource
        # Per-file failure isolation: one bad resource shouldn't kill
        # the whole batch. Return None and let caller fall back.
        return Translation(
            service_name=getattr(mod, "SERVICE_NAME", "unknown"),
            aws_inputs_hcl="# translation failed; review source inputs above\n",
            notes=[f"translate-error: {type(e).__name__}: {e}"],
        )


def all_aws_module_specs() -> List[AWSModuleSpec]:
    """Every AWS module body our translators emit.

    Used by terragrunt_emitter to write `target/modules/<service>/...`
    files. De-duplicated by service_name (eip is shared by two GCP
    types, for example).
    """
    seen = set()
    out: List[AWSModuleSpec] = []
    all_modules = (
        # Tier B (initial 7)
        gcs_to_s3, eip, elasticache, nat_gateway, rds, ec2, sns_sqs,
        # Tier 1 expansion + subnet (architectural-gap fix)
        secrets, ecr, acm, route53, vpc, security_group, subnet,
        # Tier 2 expansion
        waf, log_sink, eventbridge_scheduler,
    )
    for mod in all_modules:
        spec = mod.aws_module_spec()
        if spec.service_name in seen:
            continue
        seen.add(spec.service_name)
        out.append(spec)
    return out


def covered_gcp_types() -> List[str]:
    """The GCP tf_types for which Design-phase translation is in place."""
    return sorted(TRANSLATORS.keys())
