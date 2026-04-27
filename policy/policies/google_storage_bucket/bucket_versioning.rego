# bucket_versioning.rego
#
# Versioning MUST be enabled. Without it:
#   - accidental overwrites are unrecoverable
#   - ransomware that re-encrypts objects in place leaves no rollback
#   - lifecycle policies that "delete after N days" become irreversible
#
# Cloud field: versioning.enabled (must be true)
# Severity: HIGH -- direct data-loss exposure.
#
# --- Provenance (P4-PRE 2026-04-27) ----------------------------------
# Source:   NONE -- GoogleCloudPlatform/policy-library archived
#           library never wrote a versioning rule. Notable absence;
#           we keep ours sourced from industry consensus + the AWS
#           sibling (CIS AWS 2.1.3 covers S3 bucket versioning).
# Standard: Industry consensus / data-loss prevention. No direct
#           CIS GCP control numbers bucket versioning.
# NIST:     SP 800-53 SI-12 (Information Management and Retention) +
#           CP-9 (System Backup).
# Default:  Require versioning.enabled == true.
# ---------------------------------------------------------------------

package main

deny[msg] {
    not input.versioning.enabled
    msg := sprintf(
        "[HIGH][bucket_versioning] bucket %s does not have object versioning enabled",
        [input.name],
    )
}
