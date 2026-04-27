# pubsub_sub_iam_no_allusers.rego
# Source: NONE (Pub/Sub not covered in GoogleCloudPlatform/policy-library archived library)
# Standard: Industry consensus (no anonymous subscribe) | NIST SP 800-53 AC-3 (Access Enforcement)
# Default: Deny when iam_policy.bindings has any role bound to allUsers / allAuthenticatedUsers
# Same canonical-public-principal sentinels mined from storage_world_readable_v1 in P4-PRE.

package main

public_principals := {"allUsers", "allAuthenticatedUsers"}

deny[msg] {
    iam := object.get(input, "iam_policy", {})
    binding := object.get(iam, "bindings", [])[_]
    member := binding.members[_]
    member == public_principals[_]
    msg := sprintf(
        "[HIGH][pubsub_sub_iam_no_allusers] Pub/Sub subscription %s has public principal '%s' bound on role '%s' -- anonymous consumers can drain messages (data exfiltration vector)",
        [input.name, member, binding.role],
    )
}
