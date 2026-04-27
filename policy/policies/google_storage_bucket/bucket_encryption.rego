# bucket_encryption.rego
#
# Every bucket MUST use customer-managed encryption keys (CMEK).
# Google's default Google-managed keys satisfy "encrypted at rest" but do
# NOT satisfy regulated controls (PCI DSS 3.5, HIPAA Security Rule, SOC2
# CC6.1). CMEK requires the customer to control rotation + revocation.
#
# Cloud field: encryption.defaultKmsKeyName
# Severity: HIGH -- failed audit material on every regulated workload.
#
# --- Provenance (P4-PRE 2026-04-27) ----------------------------------
# Source:   GoogleCloudPlatform/policy-library (archived 2025-08-20)
#           gcp_storage_cmek_encryption_v1.yaml ->
#           GCPStorageCMEKEncryptionConstraintV1
# Standard: CIS Controls v8 3.11 (Encrypt Sensitive Data at Rest).
#           No specific CIS GCP control numbers bucket-level CMEK
#           directly (covered under broader "encrypt at rest").
# NIST:     SP 800-53 SC-28 (Protection of Information at Rest).
# Default:  Require encryption.defaultKmsKeyName non-empty (matches
#           Google's intent verbatim).
#
# P4-PRE applied:
#   * Helper-function pattern with defensive defaulting via OPA's
#     built-in `object.get()`. Catches BOTH "no encryption block" AND
#     "encryption block but no key" uniformly. Previously, our
#     `not input.encryption.defaultKmsKeyName` was fragile across
#     `null` vs absent vs `{}` shapes for the parent.
# ---------------------------------------------------------------------

package main

# Helper: defensively extract the bucket's default KMS key name.
# Returns "" when EITHER the encryption block is absent OR the
# defaultKmsKeyName field within it is absent. Mirrors Google's
# default_kms_key_name() helper.
default_kms_key_name := name {
    encryption := object.get(input, "encryption", {})
    name := object.get(encryption, "defaultKmsKeyName", "")
}

deny[msg] {
    default_kms_key_name == ""
    msg := sprintf(
        "[HIGH][bucket_encryption] bucket %s has no customer-managed encryption key (encryption.defaultKmsKeyName must be set)",
        [input.name],
    )
}
