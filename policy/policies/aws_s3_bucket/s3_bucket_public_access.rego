# s3_bucket_public_access.rego
#
# Every S3 bucket MUST have ALL FOUR Public Access Block settings enabled.
# This is THE one S3 setting that has caused the most data leaks of any
# AWS configuration in the platform's history (Capital One, Verizon,
# Accenture, dozens of others). The four settings are:
#
#     BlockPublicAcls          -> bucket ACLs cannot grant public read/write
#     IgnorePublicAcls         -> existing public ACLs are ignored
#     BlockPublicPolicy        -> bucket policy cannot grant public access
#     RestrictPublicBuckets    -> cross-account public principals blocked
#
# All four MUST be true for the bucket to be safely non-public. Three
# out of four leaves a hole that the missing one was specifically
# designed to close.
#
# AWS analogue of GCP's `bucket_public_access` rule (which checks
# `iamConfiguration.uniformBucketLevelAccess.enabled` -- same goal,
# AWS just splits it across 4 boolean knobs).
#
# Snapshot fields (aws s3api get-public-access-block output):
#   PublicAccessBlockConfiguration.BlockPublicAcls
#   PublicAccessBlockConfiguration.IgnorePublicAcls
#   PublicAccessBlockConfiguration.BlockPublicPolicy
#   PublicAccessBlockConfiguration.RestrictPublicBuckets
#
# Severity: HIGH -- the canonical "data breach in the morning paper"
# misconfiguration. Treat any deviation as a P0.

package main

# All four sub-flags must individually be true. Any single false (or
# absent) flag triggers a violation pointing at that specific flag so
# the operator knows what to fix.
required_flags := [
    "BlockPublicAcls",
    "IgnorePublicAcls",
    "BlockPublicPolicy",
    "RestrictPublicBuckets",
]

deny[msg] {
    flag := required_flags[_]
    not flag_enabled(flag)
    msg := sprintf(
        "[HIGH][s3_bucket_public_access] bucket %s has PublicAccessBlockConfiguration.%s != true (all four must be true to safely block public access)",
        [input.name, flag],
    )
}

flag_enabled(flag) {
    input.PublicAccessBlockConfiguration[flag] == true
}
