# network_routing_mode_regional.rego
# Source: GCP policy-library (archived 2025-08-20) gcp_network_routing_v1
# Standard: Industry consensus (blast-radius containment) | NIST SP 800-53 SC-7
# Default: Require routingConfig.routingMode == "REGIONAL" (Google's archive default was GLOBAL/parameterized; we choose REGIONAL for tighter blast radius)
# See docs/policy_provenance.md for full mining details.

package main

# Helper: defensively read the routing mode. Returns "" when the
# routingConfig block or routingMode field is absent so the inequality
# check fires (missing == not REGIONAL).
network_routing_mode := mode {
    rc := object.get(input, "routingConfig", {})
    mode := object.get(rc, "routingMode", "")
}

deny[msg] {
    network_routing_mode != "REGIONAL"
    msg := sprintf(
        "[MED][network_routing_mode_regional] network %s uses routingMode '%v' (must be REGIONAL to limit cross-region route propagation blast radius)",
        [input.name, network_routing_mode],
    )
}
