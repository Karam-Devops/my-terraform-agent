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
