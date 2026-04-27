# pubsub_topic_iam_no_allusers.rego
# Source: NONE (Pub/Sub not covered in GoogleCloudPlatform/policy-library archived library)
# Standard: Industry consensus (no anonymous publish) | NIST SP 800-53 AC-3 (Access Enforcement)
# Default: Deny when iam_policy.bindings has any role bound to allUsers / allAuthenticatedUsers
# See docs/policy_provenance.md for full mining details.

package main

# Public principals canonical strings (mined verbatim from Google's
# storage_world_readable template in P4-PRE -- same identity sentinels
# apply to every IAM-bearing GCP resource).
public_principals := {"allUsers", "allAuthenticatedUsers"}

deny[msg] {
    iam := object.get(input, "iam_policy", {})
    binding := object.get(iam, "bindings", [])[_]
    member := binding.members[_]
    member == public_principals[_]
    msg := sprintf(
        "[HIGH][pubsub_topic_iam_no_allusers] Pub/Sub topic %s has public principal '%s' bound on role '%s' -- anonymous publish/subscribe possible (data exfiltration vector)",
        [input.name, member, binding.role],
    )
}
