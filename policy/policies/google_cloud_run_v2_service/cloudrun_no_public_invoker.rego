# cloudrun_no_public_invoker.rego
# Source: NONE (Cloud Run not covered in GoogleCloudPlatform/policy-library archived library)
# Standard: CIS Controls v8 6.x (Access Control Management) | NIST SP 800-53 AC-3 (Access Enforcement)
# Default: Deny when iam_policy.bindings has roles/run.invoker bound to allUsers / allAuthenticatedUsers (Cloud Run service must require authentication)
# Industry source: Google Cloud "Securing Cloud Run services" Best Practices.
# See docs/policy_provenance.md for full mining details.

package main

# Public principals canonical strings (verbatim from Google's
# storage_world_readable template, mined P4-PRE).
public_principals := {"allUsers", "allAuthenticatedUsers"}

deny[msg] {
    iam := object.get(input, "iam_policy", {})
    binding := object.get(iam, "bindings", [])[_]
    binding.role == "roles/run.invoker"
    member := binding.members[_]
    member == public_principals[_]
    msg := sprintf(
        "[HIGH][cloudrun_no_public_invoker] Cloud Run service %s grants roles/run.invoker to public principal '%s' -- service is invokable without authentication; remove the binding or restrict to specific identities",
        [input.name, member],
    )
}
