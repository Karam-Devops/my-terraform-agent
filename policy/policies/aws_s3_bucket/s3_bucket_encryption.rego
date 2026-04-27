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
