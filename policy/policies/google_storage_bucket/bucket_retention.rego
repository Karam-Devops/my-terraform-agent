# bucket_retention.rego
#
# Soft-delete retention MUST be at least 7 days (604,800 seconds).
# Bridges the gap between "user accidentally deletes object" and
# "operator notices and recovers". GCS default is 7d so this also acts
# as a "no-one disabled it" guard.
#
# Cloud field: softDeletePolicy.retentionDurationSeconds
#   - GCP returns int64 fields as JSON strings, so the value here is a
#     string like "604800". Rego's `to_number` handles either form.
#
# Severity: MED -- recoverable via GCS support ticket if missed, but a
# real operational pain point.
#
# --- Provenance (P4-PRE 2026-04-27) ----------------------------------
# Source:   GoogleCloudPlatform/policy-library (archived 2025-08-20)
#           gcp_storage_bucket_retention_v1.yaml ->
#           GCPStorageBucketRetentionConstraintV1
#           [DIFFERENT CONTROL AXIS: Google's template covers
#           OBJECT LIFECYCLE retention (lifecycle.rule[].action.type
#           == "Delete" + age conditions). Ours covers SOFT-DELETE
#           retention -- the recovery window after explicit delete.
#           Both are valid + complementary controls. We keep ours;
#           a Phase 4 sibling rule could add Google's coverage as
#           bucket_object_lifecycle.rego.]
# Standard: No specific CIS GCP control. NIST SI-12 covers
#           "Information Management and Retention".
# NIST:     SP 800-53 SI-12.
# Default:  604800 seconds = 7 days (matches GCS default; absence
#           of an explicit retention policy is treated as "someone
#           disabled the default" and denied).
#
# Phase 4 candidates documented in docs/policy_provenance.md:
#   * Bidirectional bounds: Google parameterizes BOTH
#     minimum_retention_days AND maximum_retention_days. The max is
#     a cost-control angle (beyond N days, retention costs balloon).
#   * NEW SIBLING RULE bucket_object_lifecycle.rego: Google's
#     template covers OBJECT lifecycle Delete actions with age,
#     createdBefore, and numNewerVersions conditions. Time
#     conversion constant they use:
#         ns_in_days = ((((24 * 60) * 60) * 1000) * 1000) * 1000
# ---------------------------------------------------------------------

package main

# Helper: coerce the value to a number whether it's a JSON string or int.
retention_seconds := n {
    n := to_number(input.softDeletePolicy.retentionDurationSeconds)
}

deny[msg] {
    not input.softDeletePolicy.retentionDurationSeconds
    msg := sprintf(
        "[MED][bucket_retention] bucket %s has no soft-delete policy configured",
        [input.name],
    )
}

deny[msg] {
    input.softDeletePolicy.retentionDurationSeconds
    retention_seconds < 604800
    msg := sprintf(
        "[MED][bucket_retention] bucket %s has soft-delete retention below 7 days (%v seconds, must be >= 604800)",
        [input.name, retention_seconds],
    )
}
