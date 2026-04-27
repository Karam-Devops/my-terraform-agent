# sql_ssl_required.rego
# Source: GCP policy-library (archived 2025-08-20) gcp_sql_ssl_v1
# Standard: CIS GCP 6.4 | NIST SP 800-53 SC-8 (Transmission Confidentiality and Integrity)
# Default: Require settings.ipConfiguration.requireSsl == true (forces TLS on every connection)
# See docs/policy_provenance.md for full mining details.

package main

deny[msg] {
    settings := object.get(input, "settings", {})
    ip_config := object.get(settings, "ipConfiguration", {})
    not object.get(ip_config, "requireSsl", false) == true
    msg := sprintf(
        "[HIGH][sql_ssl_required] Cloud SQL instance %s does not require SSL (settings.ipConfiguration.requireSsl must be true) -- credentials and queries can be MITM'd in plaintext (CIS GCP 6.4)",
        [input.name],
    )
}
