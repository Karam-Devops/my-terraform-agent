# disk_cmek_required.rego
# Source: GCP policy-library (archived 2025-08-20) gcp_cmek_settings_v1 [proxy match: ours targets the disk's diskEncryptionKey.kmsKeyName, Google's targets the CryptoKey]
# Standard: CIS GCP 4.7 | NIST SP 800-53 SC-28, SC-12
# Default: Require diskEncryptionKey.kmsKeyName non-empty (CMEK; rejects Google-managed default)
# See docs/policy_provenance.md for full mining details.

package main

# Helper: defensively read the disk's KMS key reference. Returns "" when
# either the encryption block is absent OR the kmsKeyName field within
# it is absent. Mirrors gce_disk_encryption.rego's pattern.
disk_kms_key_name := name {
    enc := object.get(input, "diskEncryptionKey", {})
    name := object.get(enc, "kmsKeyName", "")
}

deny[msg] {
    disk_kms_key_name == ""
    msg := sprintf(
        "[HIGH][disk_cmek_required] disk %s has no customer-managed encryption key (diskEncryptionKey.kmsKeyName must be set) -- regulated workloads require CMEK (CIS GCP 4.7)",
        [input.name],
    )
}
