# s3_bucket_retention.rego
#
# Every S3 bucket SHOULD have at least one of:
#   * Object Lock enabled (write-once-read-many; protects against
#     deletion + ransomware overwrite for the lock duration), OR
#   * MFA Delete enabled on the versioning configuration (deletion
#     of versioned objects requires an MFA-authenticated request)
#
# Either alone provides the "you can't lose this on a bad day" guarantee
# that GCP's bucket_retention rule (soft-delete policy) provides via
# server-side undelete. AWS doesn't have a direct soft-delete equivalent
# at the bucket layer, so we accept either Object Lock OR MFA Delete as
# the protective control.
#
# AWS analogue of GCP's `bucket_retention` rule (which checks
# `softDeletePolicy`). Severity matches: MED, not HIGH, because not
# every workload needs full undelete (e.g. ephemeral build caches,
# CDN-edge data) -- it depends on the data classification.
#
# Snapshot fields (aws s3api outputs joined):
#   ObjectLockConfiguration.ObjectLockEnabled         ("Enabled" or absent)
#   VersioningConfiguration.MFADelete                  ("Enabled" or absent)
#
# Severity: MED -- protective control gap, not an immediate exposure.

package main

deny[msg] {
    not object_lock_enabled
    not mfa_delete_enabled
    msg := sprintf(
        "[MED][s3_bucket_retention] bucket %s has neither Object Lock nor MFA Delete enabled (one of ObjectLockConfiguration.ObjectLockEnabled=Enabled or VersioningConfiguration.MFADelete=Enabled is required)",
        [input.name],
    )
}

object_lock_enabled {
    input.ObjectLockConfiguration.ObjectLockEnabled == "Enabled"
}

mfa_delete_enabled {
    input.VersioningConfiguration.MFADelete == "Enabled"
}
