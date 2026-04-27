# cluster_master_authorized_networks.rego
# Source: GCP policy-library (archived 2025-08-20) gcp_gke_master_authorized_networks_enabled_v1
# Standard: CIS GCP 7.x (master access) | NIST SP 800-53 SC-7 (Boundary Protection)
# Default: Require masterAuthorizedNetworksConfig.enabled == true (master endpoint MUST NOT be reachable from 0.0.0.0/0)
# See docs/policy_provenance.md for full mining details.

package main

deny[msg] {
    man := object.get(input, "masterAuthorizedNetworksConfig", {})
    not object.get(man, "enabled", false) == true
    msg := sprintf(
        "[HIGH][cluster_master_authorized_networks] cluster %s does not have Master Authorized Networks enabled (masterAuthorizedNetworksConfig.enabled must be true) -- API server reachable from 0.0.0.0/0 (CIS GCP 7.x)",
        [input.name],
    )
}
