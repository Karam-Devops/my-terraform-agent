# firewall_no_open_rdp.rego
# Source: GCP policy-library (archived 2025-08-20) gcp_restricted_firewall_rules_v1 [derived]
# Standard: CIS GCP 3.7 | NIST SP 800-53 SC-7
# Default: Deny INGRESS+enabled+0.0.0.0/0+TCP/3389 (CIS-aligned, sibling of no_open_ssh)
# See docs/policy_provenance.md for full mining details + future-work notes.

package main

deny[msg] {
    input.direction == "INGRESS"
    firewall_enabled
    sources_open_to_internet
    allows_tcp_port_to_world("3389")
    msg := sprintf(
        "[HIGH][firewall_no_open_rdp] firewall %s allows RDP (TCP/3389) ingress from 0.0.0.0/0 -- restrict sourceRanges (CIS GCP 3.7)",
        [input.name],
    )
}
