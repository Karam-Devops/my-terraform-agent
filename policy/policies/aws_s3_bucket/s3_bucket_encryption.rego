# s3_bucket_encryption.rego
#
# Every S3 bucket MUST have server-side encryption configured with a
# customer-managed KMS key (SSE-KMS), not the default AWS-managed key
# (SSE-S3 / AES256). AWS enables SSE-S3 by default since 2023, but
# regulated controls (PCI DSS 3.5, HIPAA Security Rule, SOC2 CC6.1)
# require the customer to control rotation + revocation -- i.e. SSE-KMS
# with an aws_kms_key the customer owns.
#
# AWS analogue of GCP's `bucket_encryption` rule (which checks
# `encryption.defaultKmsKeyName` -- same intent, different field shape).
#
# Snapshot fields (aws s3api get-bucket-encryption output):
#   ServerSideEncryptionConfiguration.Rules[]
#       .ApplyServerSideEncryptionByDefault.SSEAlgorithm   ("aws:kms" required)
#       .ApplyServerSideEncryptionByDefault.KMSMasterKeyID (must be set)
#
# Severity: HIGH -- audit-failing on every bucket holding regulated data.
#
# --- Provenance (P4-PRE 2026-04-27) ----------------------------------
# Source:   GoogleCloudPlatform/policy-library has no AWS templates.
#           Cross-reference: GCP sibling rule lives at
#           policy/policies/google_storage_bucket/bucket_encryption.rego.
# Standard: CIS AWS Foundations Benchmark 2.1.1 -- "Ensure all S3
#           buckets employ encryption-at-rest" (the canonical CIS
#           rule for this control).
# NIST:     SP 800-53 SC-28 (Protection of Information at Rest).
# Default:  Require ServerSideEncryptionConfiguration with at least
#           one rule where SSEAlgorithm == "aws:kms" AND
#           KMSMasterKeyID is non-empty (CMEK / customer-managed).
#           STRICTER than CIS AWS 2.1.1's baseline (which accepts
#           SSE-S3 / AES256), matching our GCP CMEK posture for
#           regulated-data parity.
# ---------------------------------------------------------------------

package main

# Set of indices of rules that satisfy CMEK requirements.
cmek_rules[i] {
    rule := input.ServerSideEncryptionConfiguration.Rules[i]
    rule.ApplyServerSideEncryptionByDefault.SSEAlgorithm == "aws:kms"
    rule.ApplyServerSideEncryptionByDefault.KMSMasterKeyID
    rule.ApplyServerSideEncryptionByDefault.KMSMasterKeyID != ""
}

deny[msg] {
    count(cmek_rules) == 0
    msg := sprintf(
        "[HIGH][s3_bucket_encryption] bucket %s has no customer-managed KMS encryption (ServerSideEncryptionConfiguration with SSEAlgorithm=aws:kms + KMSMasterKeyID required)",
        [input.name],
    )
}
