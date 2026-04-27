# sql_no_public_ip.rego
# Source: GCP policy-library (archived 2025-08-20) gcp_sql_public_ip_v1
# Standard: CIS GCP 6.5 | NIST SP 800-53 SC-7 (Boundary Protection)
# Default: Deny when settings.ipConfiguration.ipv4Enabled == true (databases must use Private IP only)
# See docs/policy_provenance.md for full mining details.

package main

deny[msg] {
    settings := object.get(input, "settings", {})
    ip_config := object.get(settings, "ipConfiguration", {})
    object.get(ip_config, "ipv4Enabled", true) == true
    msg := sprintf(
        "[HIGH][sql_no_public_ip] Cloud SQL instance %s has public IPv4 enabled (settings.ipConfiguration.ipv4Enabled == true) -- use Private Service Connect or Private IP only (CIS GCP 6.5)",
        [input.name],
    )
}
