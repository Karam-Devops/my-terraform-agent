# bucket_versioning.rego
#
# Versioning MUST be enabled. Without it:
#   - accidental overwrites are unrecoverable
#   - ransomware that re-encrypts objects in place leaves no rollback
#   - lifecycle policies that "delete after N days" become irreversible
#
# Cloud field: versioning.enabled (must be true)
# Severity: HIGH — direct data-loss exposure.

package main

deny[msg] {
    not input.versioning.enabled
    msg := sprintf(
        "[HIGH][bucket_versioning] bucket %s does not have object versioning enabled",
        [input.name],
    )
}
