# firewall_logs_enabled.rego
# Source: GCP policy-library (archived 2025-08-20) gcp_network_enable_firewall_logs_v1
# Standard: CIS GCP 3.x (firewall logging) | NIST SP 800-53 AU-12
# Default: Deny when logConfig.enable != true (matches Google's archive default behavior)
# See docs/policy_provenance.md for full mining details.

package main

deny[msg] {
    log_config := object.get(input, "logConfig", {})
    not object.get(log_config, "enable", false) == true
    msg := sprintf(
        "[MED][firewall_logs_enabled] firewall %s does not have logging enabled (logConfig.enable must be true) -- audit trail gap (CIS GCP 3.x)",
        [input.name],
    )
}
