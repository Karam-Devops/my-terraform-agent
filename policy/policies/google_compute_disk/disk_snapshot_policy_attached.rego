# disk_snapshot_policy_attached.rego
# Source: GCP policy-library (archived 2025-08-20) gcp_compute_disk_resource_policies_v1 [derived]
# Standard: Industry consensus (data-loss prevention) | NIST SP 800-53 CP-9 (System Backup)
# Default: Require at least one entry in resourcePolicies (Google's template parameterizes; we hardcode "non-empty" as the floor)
# See docs/policy_provenance.md for full mining details.

package main

# Helper: defensively read the disk's resource policies list. Returns []
# when the field is absent so the empty-list check below works uniformly.
disk_resource_policies := lst {
    lst := object.get(input, "resourcePolicies", [])
}

deny[msg] {
    count(disk_resource_policies) == 0
    msg := sprintf(
        "[MED][disk_snapshot_policy_attached] disk %s has no snapshot/backup policy attached (resourcePolicies is empty) -- attach at least one resource policy for scheduled snapshots (NIST CP-9)",
        [input.name],
    )
}
