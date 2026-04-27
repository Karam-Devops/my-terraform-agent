# subnet_flow_logs_enabled.rego
# Source: GCP policy-library (archived 2025-08-20) gcp_network_enable_flow_logs_v1
# Standard: CIS GCP 3.8 | NIST SP 800-53 AU-12 (Audit Generation)
# Default: Deny when logConfig.enable != true AND enableFlowLogs (legacy) != true; exempt managed-proxy purposes per Google's template
# See docs/policy_provenance.md for full mining details.

package main

# Helper: subnetwork purposes that legitimately can't carry flow logs
# (Google's template exempts these). Internal HTTPS load balancer
# proxies and regional managed proxies are control-plane subnets
# without traffic flow to log.
proxy_only_purposes := {"REGIONAL_MANAGED_PROXY", "INTERNAL_HTTPS_LOAD_BALANCER"}

is_proxy_only_subnet {
    object.get(input, "purpose", "") == proxy_only_purposes[_]
}

# Helper: flow logs enabled via either modern (logConfig.enable) or
# legacy (enableFlowLogs) field. Two paths because Google's template
# checks both -- older subnetworks may carry the legacy field set.
flow_logs_enabled {
    log_config := object.get(input, "logConfig", {})
    object.get(log_config, "enable", false) == true
}

flow_logs_enabled {
    object.get(input, "enableFlowLogs", false) == true
}

deny[msg] {
    not is_proxy_only_subnet
    not flow_logs_enabled
    msg := sprintf(
        "[MED][subnet_flow_logs_enabled] subnetwork %s does not have VPC flow logs enabled (logConfig.enable must be true) -- network audit trail gap (CIS GCP 3.8)",
        [input.name],
    )
}
