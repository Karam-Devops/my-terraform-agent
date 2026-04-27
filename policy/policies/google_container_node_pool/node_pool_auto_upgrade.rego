# node_pool_auto_upgrade.rego
# Source: GCP policy-library (archived 2025-08-20) gcp_gke_node_auto_upgrade_v1
# Standard: CIS GCP 7.x (timely security patching) | NIST SP 800-53 SI-2 (Flaw Remediation)
# Default: Require management.autoUpgrade == true (Kubernetes CVEs land monthly; manual upgrade lags create critical exposure windows)
# See docs/policy_provenance.md for full mining details.

package main

deny[msg] {
    mgmt := object.get(input, "management", {})
    not object.get(mgmt, "autoUpgrade", false) == true
    msg := sprintf(
        "[HIGH][node_pool_auto_upgrade] node pool %s does not have auto-upgrade enabled (management.autoUpgrade must be true) -- Kubernetes CVEs accumulate without timely upgrade (CIS GCP 7.x)",
        [input.name],
    )
}
