# cluster_legacy_abac_disabled.rego
# Source: GCP policy-library (archived 2025-08-20) gcp_gke_legacy_abac_v1
# Standard: CIS GCP 7.x (RBAC over ABAC) | NIST SP 800-53 AC-3 (Access Enforcement)
# Default: Deny when legacyAbac.enabled == true (RBAC must be the sole authorization model)
# See docs/policy_provenance.md for full mining details.

package main

deny[msg] {
    legacy := object.get(input, "legacyAbac", {})
    object.get(legacy, "enabled", false) == true
    msg := sprintf(
        "[HIGH][cluster_legacy_abac_disabled] cluster %s has legacy ABAC enabled (legacyAbac.enabled == true) -- legacy authorization grants overly-permissive default roles; RBAC alone should be used (CIS GCP 7.x)",
        [input.name],
    )
}
