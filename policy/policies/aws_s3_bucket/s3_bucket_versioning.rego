# s3_bucket_versioning.rego
#
# Every S3 bucket MUST have versioning enabled. Without versioning, a
# single accidental DELETE or overwrite is unrecoverable -- there's no
# previous-version history to roll back to. With versioning enabled,
# every write produces a new version and old versions stay until
# explicitly purged.
#
# AWS analogue of GCP's `bucket_versioning` rule.
#
# Snapshot field (aws s3api get-bucket-versioning output):
#   Status   "Enabled"   -> versioning ON  (compliant)
#            "Suspended" -> versioning was on, now off; existing versions
#                           preserved but new writes don't create versions
#                           (violation -- a partial state)
#            absent      -> versioning was never configured (violation)
#
# Severity: HIGH -- one accidental delete from any client SDK / IAM
# principal with s3:DeleteObject can permanently lose the object. This
# is the rule that prevents "we lost the entire customer database in
# one click" incidents.
#
# --- Provenance (P4-PRE 2026-04-27) ----------------------------------
# Source:   GoogleCloudPlatform/policy-library has no AWS templates.
#           Note: Google's archived library never wrote a versioning
#           rule for GCS either (notable absence). Cross-reference:
#           GCP sibling rule lives at
#           policy/policies/google_storage_bucket/bucket_versioning.rego
#           (industry-consensus sourced).
# Standard: CIS AWS Foundations Benchmark 2.1.3 -- "Ensure MFA Delete
#           is enabled on S3 buckets" (CIS pairs versioning with MFA
#           Delete; we cover MFA Delete in s3_bucket_retention.rego
#           and versioning here).
# NIST:     SP 800-53 SI-12 (Information Management and Retention) +
#           CP-9 (System Backup).
# Default:  Require VersioningConfiguration.Status == "Enabled".
#           "Suspended" is treated as a violation (versioning was on,
#           now off; existing versions preserved but new writes don't
#           create versions -- a partial / inconsistent state).
# ---------------------------------------------------------------------

package main

deny[msg] {
    not versioning_enabled
    msg := sprintf(
        "[HIGH][s3_bucket_versioning] bucket %s does not have versioning enabled (VersioningConfiguration.Status must be \"Enabled\")",
        [input.name],
    )
}

versioning_enabled {
    input.VersioningConfiguration.Status == "Enabled"
}
