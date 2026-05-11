"""Compliance profile defaults toggled across translators.

A compliance profile bakes opinionated security + audit defaults into
every translator's output, so the operator picks "HIPAA" once and the
emitted AWS code is hardened by default — instead of hand-editing each
resource post-hoc.

How translators consume the profile:

    from migrator.translate.compliance_profiles import get_defaults

    def translate(resource, *, compliance_profile="none"):
        defaults = get_defaults(compliance_profile, service="s3")
        block_public_access = defaults.get("block_public_access", False)
        ...

Translators that haven't been wired to consume the profile yet still
work — they just emit their neutral defaults. We add coverage
translator-by-translator. Today: gcs_to_s3 is wired; others will land
in subsequent Week 2 commits.

Profile choices match the four compliance regimes most relevant to
healthcare + financial customers:

  * "none"  — Migrator's neutral defaults; operator hardens manually.
  * "hipaa" — HIPAA Security Rule (45 CFR § 164.308-312). Healthcare
              PHI requires encryption-at-rest, encryption-in-transit,
              audit logging, deletion protection on production data.
  * "soc2"  — SOC 2 Type II controls. Similar to HIPAA but less
              prescriptive on KMS specifically; emphasis on audit
              trail + access logging.
  * "pci"   — PCI DSS 4.0. Cardholder data; even stricter on
              encryption (KMS required everywhere) + segmentation.
"""

from __future__ import annotations

from typing import Dict


# Profile-name → {service-name → {default-key: value, ...}}
#
# Service names match the translator SERVICE_NAME constants where
# practical (s3-bucket, rds-postgres, etc.), but can also be a coarser
# logical service (e.g., "s3" applies to anything S3-related).
#
# Translators look up `get_defaults(profile, service)` and merge those
# defaults into their per-resource translation output. Customer-provided
# values in the source repo always win — the profile only fills GAPS.
_PROFILES: Dict[str, Dict[str, Dict]] = {
    "none": {},

    "hipaa": {
        "s3": {
            "block_public_access": True,    # 45 CFR § 164.312(e)(1) — public PHI exposure
            "versioning":          True,    # § 164.308(a)(7)(ii)(A) — data backup
            "kms_encryption":      True,    # § 164.312(a)(2)(iv) — encryption-at-rest
            "force_destroy":       False,   # protect against accidental delete of PHI
            "access_logging":      True,    # § 164.312(b) — audit controls
        },
        "rds": {
            "deletion_protection":          True,    # PHI protected from accidental drop
            "storage_encrypted":            True,    # encryption-at-rest
            "backup_retention_days":        35,      # § 164.316(b)(2) — 6yr retention; 35d is daily windows
            "performance_insights_enabled": True,    # audit + perf monitoring
            "iam_database_authentication":  True,    # eliminate plaintext credentials
            "monitoring_interval":          60,      # enhanced monitoring (1-minute resolution)
        },
        "vpc": {
            "enable_flow_logs": True,      # § 164.312(b) — network audit logging
        },
        "secrets": {
            "kms_encryption":         True,
            "automatic_rotation":     True,    # § 164.308(a)(5)(ii)(D) — periodic credential change
            "rotation_period_days":   90,
        },
        "eks": {
            "endpoint_public_access":   False,   # private API server only
            "encryption_secrets":       True,    # envelope-encrypt K8s Secrets
            "logging_enabled_types":    ["api", "audit", "authenticator"],
            "irsa_required":            True,    # no static IAM credentials on nodes
        },
        "alb": {
            "drop_invalid_header_fields": True,
            "access_logs_enabled":        True,
            "min_tls_version":            "TLSv1.2_2021",
        },
    },

    "soc2": {
        # Less prescriptive than HIPAA on KMS specifically. Focus on
        # audit trail + access logging + change management.
        "s3": {
            "block_public_access": True,
            "versioning":          True,
            "force_destroy":       False,
            "access_logging":      True,
        },
        "rds": {
            "deletion_protection":          True,
            "storage_encrypted":            True,
            "backup_retention_days":        14,
            "performance_insights_enabled": True,
        },
        "vpc": {
            "enable_flow_logs": True,
        },
        "eks": {
            "endpoint_public_access":   False,
            "logging_enabled_types":    ["api", "audit"],
        },
        "alb": {
            "access_logs_enabled":        True,
            "min_tls_version":            "TLSv1.2_2021",
        },
    },

    "pci": {
        # Stricter than HIPAA on KMS — REQUIRED everywhere. Segmentation
        # requirements drive private endpoints + restricted ingress.
        "s3": {
            "block_public_access": True,
            "versioning":          True,
            "kms_encryption":      True,
            "force_destroy":       False,
            "access_logging":      True,
        },
        "rds": {
            "deletion_protection":          True,
            "storage_encrypted":            True,    # PCI DSS 4.0 Req 3.5
            "backup_retention_days":        30,
            "performance_insights_enabled": True,
            "iam_database_authentication":  True,
        },
        "vpc": {
            "enable_flow_logs": True,
        },
        "secrets": {
            "kms_encryption":       True,
            "automatic_rotation":   True,
            "rotation_period_days": 90,
        },
        "eks": {
            "endpoint_public_access":   False,
            "encryption_secrets":       True,
            "logging_enabled_types":    ["api", "audit", "authenticator"],
        },
        "alb": {
            "drop_invalid_header_fields": True,
            "access_logs_enabled":        True,
            "min_tls_version":            "TLSv1.2_2021",   # PCI DSS 4.0 Req 4.2 — strong crypto
        },
    },
}


PROFILE_NAMES = tuple(_PROFILES.keys())


# Human-readable descriptions for UI tooltips.
PROFILE_DESCRIPTIONS = {
    "none":  "Migrator's neutral defaults. Operator hardens each resource manually after emission.",
    "hipaa": "HIPAA Security Rule (45 CFR § 164.308-312). Healthcare PHI: KMS encryption + deletion protection + audit logging + private endpoints.",
    "soc2":  "SOC 2 Type II controls. Audit trail + access logging + change management. KMS not strictly required (operator decides per service).",
    "pci":   "PCI DSS 4.0. Cardholder data: KMS required everywhere + strong TLS + network segmentation.",
}


def get_defaults(profile: str, service: str) -> Dict:
    """Return the profile's defaults dict for a given service name.

    Empty dict if profile doesn't exist OR profile doesn't define the
    service. Translators can safely call this with any (profile,
    service) tuple — no exceptions.

    Examples:
        get_defaults("hipaa", "s3")
        # → {"block_public_access": True, "versioning": True, ...}

        get_defaults("none", "s3")
        # → {}    (caller's per-resource defaults apply)

        get_defaults("hipaa", "unrecognized-service")
        # → {}    (translator not yet wired for this profile)
    """
    profile = (profile or "none").strip().lower()
    return dict(_PROFILES.get(profile, {}).get(service, {}))


def is_valid_profile(profile: str) -> bool:
    """True iff `profile` is a recognized name (case-insensitive)."""
    return (profile or "").strip().lower() in _PROFILES


def list_services_hardened_by(profile: str) -> list:
    """List of service names this profile applies non-trivial defaults to.

    Used by the UI to show "HIPAA hardens: s3, rds, vpc, secrets, eks, alb"
    so the operator knows what changes when they pick a profile.
    """
    profile = (profile or "none").strip().lower()
    return sorted(_PROFILES.get(profile, {}).keys())
