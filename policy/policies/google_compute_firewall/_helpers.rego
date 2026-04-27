# _helpers.rego
#
# Shared helpers for google_compute_firewall rules. Extracted in P4-5
# once the second sibling rule (no_open_rdp) made duplication concrete.
# All helpers are pure (no I/O) and live in `package main` so the
# per-rule deny[] blocks can reference them without imports.
#
# Convention: filename starts with `_` so it's visually distinct from
# the rule files it supports. conftest treats it identically -- loads
# every .rego in the dir.
#
# Field paths + sentinel values mined from
# GoogleCloudPlatform/policy-library (archived 2025-08-20)
# gcp_restricted_firewall_rules_v1.yaml. See docs/policy_provenance.md
# for the full extraction details.

package main

# Defensive list extraction. Returns [] when the field is absent so
# downstream iteration doesn't bomb on missing-parent errors. Mirrors
# Google's lib.get_default() pattern.
default_list(parent, key) := lst {
    lst := object.get(parent, key, [])
}

# A firewall rule is in effect unless explicitly disabled. Defensive
# on the missing-field case (treat missing as enabled, matching GCP's
# semantics).
firewall_enabled {
    not object.get(input, "disabled", false) == true
}

# Sources include the universal "any source" CIDR -- the canonical
# sentinel from Google's template. 0.0.0.0/0 = open to the world.
sources_open_to_internet {
    default_list(input, "sourceRanges")[_] == "0.0.0.0/0"
}

# An allowed[] entry permits TCP traffic. Two cases: explicit "tcp"
# OR the "all" wildcard (matches every protocol including TCP).
permits_tcp(allowed_entry) {
    allowed_entry.IPProtocol == "tcp"
}

permits_tcp(allowed_entry) {
    allowed_entry.IPProtocol == "all"
}

# An allowed[] entry permits the given port. Three cases:
#   (a) ports field absent entirely -> no port restriction (all open)
#   (b) ports contains the exact string match
#   (c) ports contains a range like "20-25" or "0-65535" containing it
permits_port(allowed_entry, port_str) {
    not allowed_entry.ports
}

permits_port(allowed_entry, port_str) {
    allowed_entry.ports[_] == port_str
}

permits_port(allowed_entry, port_str) {
    range_str := allowed_entry.ports[_]
    parts := split(range_str, "-")
    count(parts) == 2
    lo := to_number(parts[0])
    hi := to_number(parts[1])
    target := to_number(port_str)
    lo <= target
    target <= hi
}

# Composite: ANY allowed entry on the rule reaches the given TCP port.
# Used by per-port deny rules (firewall_no_open_ssh,
# firewall_no_open_rdp, future port-specific rules).
allows_tcp_port_to_world(port_str) {
    allowed_entry := default_list(input, "allowed")[_]
    permits_tcp(allowed_entry)
    permits_port(allowed_entry, port_str)
}
