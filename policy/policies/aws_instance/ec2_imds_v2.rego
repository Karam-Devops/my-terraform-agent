# ec2_imds_v2.rego
#
# EC2 instances MUST require IMDSv2 (token-based metadata access) instead
# of falling back to IMDSv1. IMDSv1 is the source of countless SSRF -> credential
# theft incidents (Capital One 2019 being the canonical case): an attacker
# who can make the EC2 issue an outbound HTTP request can fetch the
# instance role's credentials from 169.254.169.254 with a single GET.
# IMDSv2 requires a session token from PUT first, breaking that attack.
#
# This is the AWS analogue of GCP's "shielded VM" rule -- both are
# baseline platform-hardening that defaults to off and must be turned
# on explicitly.
#
# Snapshot field: MetadataOptions.HttpTokens
#   "required"  -> IMDSv2 enforced (compliant)
#   "optional"  -> IMDSv1 still works (violation)
#   absent      -> defaults to "optional" on instances created before
#                  the 2022 default change (violation -- treat absent as v1)
#
# Severity: MED -- not as immediately exploitable as a public IP, but
# the blast radius when it IS exploited (full instance role compromise)
# is severe. MED keeps it visible without blocking on every legacy VM
# during initial codification.

package main

deny[msg] {
    # Catches both "absent" and explicitly "optional".
    not http_tokens_required
    msg := sprintf(
        "[MED][ec2_imds_v2] instance %s does not enforce IMDSv2 (MetadataOptions.HttpTokens must be \"required\")",
        [input.name],
    )
}

http_tokens_required {
    input.MetadataOptions.HttpTokens == "required"
}
