# subnet_private_google_access.rego
# Source: GCP policy-library (archived 2025-08-20) gcp_network_enable_private_google_access_v1
# Standard: Industry consensus (egress security) | NIST SP 800-53 SC-7 (Boundary Protection)
# Default: Require privateIpGoogleAccess == true so workloads reach Google APIs without traversing public internet
# See docs/policy_provenance.md for full mining details.

package main

deny[msg] {
    not object.get(input, "privateIpGoogleAccess", false) == true
    msg := sprintf(
        "[MED][subnet_private_google_access] subnetwork %s does not have Private Google Access enabled (privateIpGoogleAccess must be true) -- VMs without external IPs cannot reach Google APIs",
        [input.name],
    )
}
