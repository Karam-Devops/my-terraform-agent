# node_pool_auto_repair.rego
# Source: GCP policy-library (archived 2025-08-20) gcp_gke_node_auto_repair_v1
# Standard: CIS GCP 7.x (node availability) | NIST SP 800-53 SI-2
# Default: Require management.autoRepair == true (failed nodes auto-replaced; manual remediation is operational toil)
# See docs/policy_provenance.md for full mining details.

package main

deny[msg] {
    mgmt := object.get(input, "management", {})
    not object.get(mgmt, "autoRepair", false) == true
    msg := sprintf(
        "[MED][node_pool_auto_repair] node pool %s does not have auto-repair enabled (management.autoRepair must be true) -- failed nodes require manual replacement (CIS GCP 7.x)",
        [input.name],
    )
}
