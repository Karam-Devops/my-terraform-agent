# gce_no_public_ip.rego
#
# Compute instances MUST NOT have an external IP attached. The presence
# of any `accessConfigs` entry on any networkInterface means the VM has
# a public NAT -- which means it's reachable from the open internet
# whether the security team knows about it or not.
#
# Standard exception path: tag the instance with `internet-facing` to
# acknowledge the risk. Untagged public IPs are the violation.
#
# Cloud fields:
#   networkInterfaces[].accessConfigs[]   (presence = public IP)
#   tags.items[]                          (look for "internet-facing")
#
# Severity: HIGH -- directly exposes the VM to internet scanning.
#
# --- Provenance (P4-PRE 2026-04-27) ----------------------------------
# Source:   GoogleCloudPlatform/policy-library (archived 2025-08-20)
#           gcp_compute_external_ip_address.yaml ->
#           GCPComputeExternalIpAccessConstraintV2
#           Google's template carries `benchmark: CIS11_5.03`
#           annotation in metadata (CIS Controls v1.1 sec 5.03 --
#           "Implement Application Layer Filtering" / network
#           boundary).
# Standard: CIS Controls v8 12.X (Network Infrastructure Management).
#           No direct CIS GCP rule; nearest are CIS GCP 3.6/3.7 for
#           firewall-side ingress controls.
# NIST:     SP 800-53 SC-7 (Boundary Protection).
# Default:  Deny presence of accessConfigs unless the instance carries
#           the `internet-facing` tag (hardcoded acknowledgment tag).
#
# Phase 4 candidates documented in docs/policy_provenance.md:
#   * Allowlist/denylist parameterization: Google supports four modes
#     (allowlist|denylist) x (exact|regex). Currently we use a single
#     hardcoded tag exception. Google's model is more flexible for
#     orgs with many internet-facing VMs (web tier) -- could
#     parameterize as:
#         mode: "allowlist" | "denylist"
#         match_mode: "exact" | "regex"
#         instances: ["web-*", "edge-*", ...]
# ---------------------------------------------------------------------

package main

# An instance is internet-tagged if it has the explicit acknowledgment tag.
internet_tagged {
    input.tags.items[_] == "internet-facing"
}

# Set of interface indices that have at least one access config (= public IP).
public_interfaces[i] {
    input.networkInterfaces[i].accessConfigs[_]
}

deny[msg] {
    count(public_interfaces) > 0
    not internet_tagged
    msg := sprintf(
        "[HIGH][gce_no_public_ip] instance %s has external IP on %d network interface(s) without the 'internet-facing' tag",
        [input.name, count(public_interfaces)],
    )
}
