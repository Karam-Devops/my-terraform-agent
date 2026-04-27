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
#
# --- Provenance (P4-PRE 2026-04-27) ----------------------------------
# Source:   GoogleCloudPlatform/policy-library has no AWS templates.
#           Cross-reference: GCP sibling rule lives at
#           policy/policies/google_storage_bucket/bucket_public_access.rego.
# Standard: CIS AWS Foundations Benchmark 2.1.5 -- "Ensure that S3
#           Buckets are configured with 'Block public access (bucket
#           settings)'" (covers all four PublicAccessBlock flags).
# NIST:     SP 800-53 AC-3 (Access Enforcement) +
#           SC-7 (Boundary Protection).
# Default:  Require all four PublicAccessBlockConfiguration flags
#           true (BlockPublicAcls, IgnorePublicAcls,
#           BlockPublicPolicy, RestrictPublicBuckets). Missing any
#           one leaves a hole that flag was specifically designed
#           to close -- 4-of-4 is the intended posture.
# ---------------------------------------------------------------------

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
