# gce_disk_encryption.rego
#
# Every disk attached to the instance MUST be encrypted with a customer-
# managed key (CMEK). Same audit rationale as bucket_encryption.rego --
# Google-managed encryption is the default but does not satisfy regulated
# controls.
#
# Cloud field: disks[].diskEncryptionKey.kmsKeyName  (must be set on every disk)
#
# Severity: HIGH -- failed-audit material on every regulated workload.
# We emit one violation per misconfigured disk so the report says exactly
# which disk to fix instead of forcing the operator to grep through the
# raw snapshot.
#
# --- Provenance (P4-PRE 2026-04-27) ----------------------------------
# Source:   GoogleCloudPlatform/policy-library (archived 2025-08-20)
#           gcp_cmek_settings_v1.yaml -> GCPCMEKSettingsConstraintV1
#           [PROXY MATCH: Google's template targets the CryptoKey
#           asset directly; ours targets the consuming Instance's disks.
#           Same intent, different validation surface.]
# Standard: CIS GCP 4.7 -- "Ensure VM disks for critical VMs are
#           encrypted with Customer-Supplied Encryption Keys (CSEK)
#           or Customer Managed Encryption Keys (CMEK)".
# NIST:     SP 800-53 SC-28 (Protection of Information at Rest) +
#           SC-12 (Cryptographic Key Establishment).
# Default:  Require diskEncryptionKey.kmsKeyName on every disk
#           (matches Google's intent: a managed key reference must
#           exist).
#
# Phase 4 candidates documented in docs/policy_provenance.md:
#   * NEW RULE google_kms_crypto_key/* package validating the KEY
#     itself: protection_level (HSM > SOFTWARE), algorithm allowlist,
#     purpose, rotation_period. Google's archived default rotation
#     was 31536000s (1 year); CIS GCP 1.10 / 4.8 recommends 90 days.
#     Sentinel: 99999999s = "never rotates" (use as fail-trigger
#     fallback when rotationPeriod field is absent).
# ---------------------------------------------------------------------

package main

# Disks that lack a customer-managed encryption key.
unencrypted_disk_indices[i] {
    disk := input.disks[i]
    not disk.diskEncryptionKey.kmsKeyName
}

deny[msg] {
    i := unencrypted_disk_indices[_]
    disk := input.disks[i]
    # Surface deviceName when present (humans recognize disks by name, not
    # index); fall back to the array index when the snapshot omits it.
    disk_label := disk_display_name(disk, i)
    msg := sprintf(
        "[HIGH][gce_disk_encryption] instance %s has disk '%s' without a customer-managed encryption key (diskEncryptionKey.kmsKeyName)",
        [input.name, disk_label],
    )
}

disk_display_name(disk, i) := name {
    name := disk.deviceName
} else := name {
    name := sprintf("disks[%d]", [i])
}
