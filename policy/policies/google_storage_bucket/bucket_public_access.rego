# bucket_public_access.rego
#
# Two controls in one file because they're conceptually the same risk:
#   1. Uniform bucket-level access (UBLA) MUST be enabled. Without it,
#      legacy ACLs can grant per-object public read independent of any
#      IAM policy you set.
#   2. Public access prevention MUST be enforced. With UBLA on but PAP
#      off, allUsers / allAuthenticatedUsers can still be granted at the
#      bucket level by an IAM mistake.
#
# Cloud fields:
#   iamConfiguration.uniformBucketLevelAccess.enabled  (must be true)
#   iamConfiguration.publicAccessPrevention            (must be "enforced")
#
# Severity: HIGH — this is the single most-cited public-bucket leak class
# (Capital One, et al). Every enterprise scanner gates on it.

package main

deny[msg] {
    not input.iamConfiguration.uniformBucketLevelAccess.enabled
    msg := sprintf(
        "[HIGH][bucket_public_access] bucket %s does not have uniform bucket-level access enabled (legacy ACLs allowed)",
        [input.name],
    )
}

deny[msg] {
    input.iamConfiguration.publicAccessPrevention != "enforced"
    msg := sprintf(
        "[HIGH][bucket_public_access] bucket %s does not enforce public access prevention (currently: %v)",
        [input.name, input.iamConfiguration.publicAccessPrevention],
    )
}
