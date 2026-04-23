# bucket_encryption.rego
#
# Every bucket MUST use customer-managed encryption keys (CMEK).
# Google's default Google-managed keys satisfy "encrypted at rest" but do
# NOT satisfy regulated controls (PCI DSS 3.5, HIPAA Security Rule, SOC2
# CC6.1). CMEK requires the customer to control rotation + revocation.
#
# Cloud field: encryption.defaultKmsKeyName
# Severity: HIGH — failed audit material on every regulated workload.

package main

deny[msg] {
    not input.encryption.defaultKmsKeyName
    msg := sprintf(
        "[HIGH][bucket_encryption] bucket %s has no customer-managed encryption key (encryption.defaultKmsKeyName must be set)",
        [input.name],
    )
}
