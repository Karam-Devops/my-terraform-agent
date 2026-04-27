# firewall_no_open_ssh.rego
# Source: GCP policy-library (archived 2025-08-20) gcp_restricted_firewall_rules_v1 [derived]
# Standard: CIS GCP 3.6 | NIST SP 800-53 SC-7
# Default: Deny INGRESS+enabled+0.0.0.0/0+TCP/22 (CIS-aligned, stricter than Google's parameterized template)
# See docs/policy_provenance.md for full mining details + future-work notes.

package main

deny[msg] {
    input.direction == "INGRESS"
    firewall_enabled
    sources_open_to_internet
    allows_tcp_port_to_world("22")
    msg := sprintf(
        "[HIGH][firewall_no_open_ssh] firewall %s allows SSH (TCP/22) ingress from 0.0.0.0/0 -- restrict sourceRanges (CIS GCP 3.6)",
        [input.name],
    )
}
