# network_no_default_vpc.rego
# Source: GCP policy-library (archived 2025-08-20) gcp_network_restrict_default_v1
# Standard: CIS GCP 3.1 | NIST SP 800-53 SC-7 (Boundary Protection)
# Default: Deny any VPC named "default" (CIS recommends deleting the auto-created default network)
# See docs/policy_provenance.md for full mining details.

package main

deny[msg] {
    input.name == "default"
    msg := sprintf(
        "[HIGH][network_no_default_vpc] network %s is the auto-created default VPC (overly-permissive firewall rules pre-applied) -- delete and use a custom VPC (CIS GCP 3.1)",
        [input.name],
    )
}
