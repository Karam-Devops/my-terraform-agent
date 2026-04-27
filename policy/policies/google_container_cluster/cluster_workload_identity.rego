# cluster_workload_identity.rego
# Source: GCP policy-library (archived 2025-08-20) gcp_gke_enable_workload_identity_v1
# Standard: CIS GCP 7.x (workload identity) | NIST SP 800-53 IA-5 (Authenticator Management)
# Default: Require workloadIdentityConfig.workloadPool (current API) OR identityNamespace (legacy beta API) be set
# See docs/policy_provenance.md for full mining details.

package main

# Helper: cluster has workload identity enabled via either field path.
# Google's template checks both because beta-era clusters carry the
# legacy identityNamespace field while modern clusters use workloadPool.
workload_identity_enabled {
    wic := object.get(input, "workloadIdentityConfig", {})
    object.get(wic, "workloadPool", "") != ""
}

workload_identity_enabled {
    wic := object.get(input, "workloadIdentityConfig", {})
    object.get(wic, "identityNamespace", "") != ""
}

deny[msg] {
    not workload_identity_enabled
    msg := sprintf(
        "[HIGH][cluster_workload_identity] cluster %s does not have Workload Identity enabled (workloadIdentityConfig.workloadPool must be set) -- pods authenticate via node SA without WI, breaking least-privilege (CIS GCP 7.x)",
        [input.name],
    )
}
