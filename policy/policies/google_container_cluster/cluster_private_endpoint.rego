# cluster_private_endpoint.rego
# Source: GCP policy-library (archived 2025-08-20) gcp_gke_private_cluster_v1
# Standard: CIS GCP 7.x (private cluster) | NIST SP 800-53 SC-7 (Boundary Protection)
# Default: Require both privateClusterConfig.enablePrivateNodes AND enablePrivateEndpoint == true (Google's archive denies if config absent; we additionally require both knobs true)
# See docs/policy_provenance.md for full mining details.

package main

# Helper: defensively read the privateClusterConfig block. Returns {}
# when absent so downstream lookups don't bomb.
private_cluster_config := cfg {
    cfg := object.get(input, "privateClusterConfig", {})
}

deny[msg] {
    not object.get(private_cluster_config, "enablePrivateNodes", false) == true
    msg := sprintf(
        "[HIGH][cluster_private_endpoint] cluster %s does not have private nodes enabled (privateClusterConfig.enablePrivateNodes must be true) -- node IPs reachable from public internet (CIS GCP 7.x)",
        [input.name],
    )
}

deny[msg] {
    object.get(private_cluster_config, "enablePrivateNodes", false) == true
    not object.get(private_cluster_config, "enablePrivateEndpoint", false) == true
    msg := sprintf(
        "[MED][cluster_private_endpoint] cluster %s has private nodes but public master endpoint exposed (privateClusterConfig.enablePrivateEndpoint must be true) -- API server reachable from internet (CIS GCP 7.x)",
        [input.name],
    )
}
