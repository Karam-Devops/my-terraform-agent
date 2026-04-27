# bucket_public_access.rego
#
# Three controls in one file because they're conceptually the same risk:
#   1. Uniform bucket-level access (UBLA) MUST be enabled. Without it,
#      legacy ACLs can grant per-object public read independent of any
#      IAM policy you set. Checked via BOTH the legacy bucketPolicyOnly
#      API AND the modern uniformBucketLevelAccess API -- a bucket may
#      have either set depending on when it was created.
#   2. Public access prevention MUST be enforced. With UBLA on but PAP
#      off, allUsers / allAuthenticatedUsers can still be granted at the
#      bucket level by an IAM mistake.
#   3. The bucket's IAM policy bindings MUST NOT include the public
#      principals "allUsers" or "allAuthenticatedUsers" directly --
#      defense-in-depth catch even if UBLA + PAP look right but the
#      bindings actually contain a public member.
#
# Cloud fields:
#   iamConfiguration.uniformBucketLevelAccess.enabled  (modern API)
#   iamConfiguration.bucketPolicyOnly.enabled          (legacy API)
#   iamConfiguration.publicAccessPrevention            (must be "enforced")
#   iam_policy.bindings[].members[]                    (no allUsers /
#                                                       allAuthenticatedUsers)
#
# Severity: HIGH -- this is the single most-cited public-bucket leak class
# (Capital One, et al). Every enterprise scanner gates on it.
#
# --- Provenance (P4-PRE 2026-04-27) ----------------------------------
# Source:   GoogleCloudPlatform/policy-library (archived 2025-08-20)
#           TWO templates absorbed:
#           * gcp_storage_bucket_world_readable_v1.yaml ->
#             GCPStorageBucketWorldReadableConstraintV1
#             (covers the IAM-binding direct check)
#           * gcp_storage_bucket_policy_only_v1.yaml ->
#             GCPStorageBucketPolicyOnlyConstraintV1
#             (covers the dual-API UBLA check)
# Standard: CIS GCP 5.1 ("Ensure Cloud Storage bucket is not
#           anonymously or publicly accessible") +
#           CIS GCP 5.2 ("Ensure Cloud Storage buckets have uniform
#           bucket-level access enabled").
# NIST:     SP 800-53 AC-3 (Access Enforcement) +
#           SC-7 (Boundary Protection).
# Default:  Deny if EITHER UBLA mechanism is unset/false OR PAP is
#           not "enforced" OR public principals appear in bindings.
#           Public principals (verbatim canonical strings from
#           Google's template):
#             "allUsers"               -- completely public
#             "allAuthenticatedUsers"  -- any authenticated Google
#                                         user (still essentially
#                                         public)
#
# P4-PRE applied:
#   * Dual-API UBLA check (legacy bucketPolicyOnly + modern
#     uniformBucketLevelAccess) -- prevents false-fire on older
#     buckets that only have the legacy field set.
#   * Direct IAM-binding scan for public principals -- defense-in-depth
#     catch if the configuration knobs look right but a binding
#     slipped through.
#
# Phase 4 candidates documented in docs/policy_provenance.md:
#   * Parameterized exemption list (Google's `exemptions: []`)
#     for buckets that may legally be public (e.g. public-website-
#     content buckets).
# ---------------------------------------------------------------------

package main

# Public principals -- the exact canonical strings from Google's
# template. Listing them here as data so the deny rule iterates
# uniformly and so a human reading the policy sees what counts as
# "public" without having to parse Rego semantics.
public_principals := {"allUsers", "allAuthenticatedUsers"}

# Helper: defensively extract the iamConfiguration block.
iam_configuration := cfg {
    cfg := object.get(input, "iamConfiguration", {})
}

# Helper: true iff either the legacy bucketPolicyOnly API or the
# modern uniformBucketLevelAccess API has enabled=true. A bucket may
# carry either depending on when it was created; a bucket with the
# legacy field set IS still UBLA-protected. Google's template
# checks both this way.
ubla_enabled {
    bpo := object.get(iam_configuration, "bucketPolicyOnly", {})
    object.get(bpo, "enabled", false) == true
}

ubla_enabled {
    ubla := object.get(iam_configuration, "uniformBucketLevelAccess", {})
    object.get(ubla, "enabled", false) == true
}

# Rule 1: UBLA must be enabled (via either API).
deny[msg] {
    not ubla_enabled
    msg := sprintf(
        "[HIGH][bucket_public_access] bucket %s does not have uniform bucket-level access enabled (legacy ACLs allowed; checked both bucketPolicyOnly and uniformBucketLevelAccess)",
        [input.name],
    )
}

# Rule 2: Public Access Prevention must be enforced.
deny[msg] {
    pap := object.get(iam_configuration, "publicAccessPrevention", "")
    pap != "enforced"
    msg := sprintf(
        "[HIGH][bucket_public_access] bucket %s does not enforce public access prevention (currently: %v)",
        [input.name, pap],
    )
}

# Rule 3 (P4-PRE NEW): direct IAM-binding scan for public principals.
# Defense-in-depth: even if UBLA + PAP look right, a binding that names
# allUsers / allAuthenticatedUsers is the smoking gun.
deny[msg] {
    bindings := object.get(input, "iam_policy", {})
    binding := object.get(bindings, "bindings", [])[_]
    member := binding.members[_]
    member == public_principals[_]
    msg := sprintf(
        "[HIGH][bucket_public_access] bucket %s has public principal '%s' bound on role '%s' (direct IAM exposure)",
        [input.name, member, binding.role],
    )
}
