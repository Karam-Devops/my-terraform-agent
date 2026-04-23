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
# Severity: MED — recoverable via GCS support ticket if missed, but a
# real operational pain point.

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
