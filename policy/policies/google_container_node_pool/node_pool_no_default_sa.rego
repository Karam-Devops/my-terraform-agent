# node_pool_no_default_sa.rego
# Source: GCP policy-library (archived 2025-08-20) gcp_gke_disable_default_service_account_v1
# Standard: CIS GCP 7.x (least-privilege workload identity) | NIST SP 800-53 AC-6 (Least Privilege)
# Default: Deny config.serviceAccount in {"default", ""} (the default Compute SA carries broad project-Editor scopes)
# See docs/policy_provenance.md for full mining details.

package main

# The default Compute Engine SA name (per project) AND the empty/missing
# field both indicate "use the default SA" -- both are violations.
# A custom dedicated SA (any other email) is the correct posture.
deny[msg] {
    cfg := object.get(input, "config", {})
    sa := object.get(cfg, "serviceAccount", "default")
    sa == "default"
    msg := sprintf(
        "[HIGH][node_pool_no_default_sa] node pool %s uses the default Compute SA (config.serviceAccount == 'default' or absent) -- breaks least-privilege; create a dedicated SA with only the scopes this pool's workloads need (CIS GCP 7.x)",
        [input.name],
    )
}
