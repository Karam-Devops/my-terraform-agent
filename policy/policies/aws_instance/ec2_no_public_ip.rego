# ec2_no_public_ip.rego
#
# EC2 instances MUST NOT have a public IPv4 address attached. The presence
# of any value in `PublicIpAddress` (or auto-assignment via the ENI's
# `Association.PublicIp`) means the instance is reachable from the open
# internet -- usually unintended, almost always a finding for any
# production workload.
#
# Standard exception path: tag the instance with `internet-facing` (Tags
# list entry with Key="internet-facing") to acknowledge the risk. Untagged
# public IPs are the violation. Mirrors the GCP `gce_no_public_ip` rule's
# acknowledgement-tag pattern.
#
# Snapshot fields (aws ec2 describe-instances output):
#   PublicIpAddress                                  (string, top-level)
#   NetworkInterfaces[].Association.PublicIp         (string, per ENI)
#   Tags[].Key                                       (look for "internet-facing")
#
# Severity: HIGH -- directly exposes the instance to internet scanning.
#
# --- Provenance (P4-PRE 2026-04-27) ----------------------------------
# Source:   GoogleCloudPlatform/policy-library has no AWS templates.
#           Cross-reference: GCP sibling rule lives at
#           policy/policies/google_compute_instance/gce_no_public_ip.rego
#           (same hardcoded "internet-facing" tag exception pattern).
# Standard: CIS Controls v8 12.X (Network Infrastructure Management).
#           No direct CIS AWS rule numbered for "no public IP" (the
#           AWS Foundations Benchmark covers security groups + NACLs
#           but not instance-level public IP).
# NIST:     SP 800-53 SC-7 (Boundary Protection).
# Default:  Deny presence of PublicIpAddress (top-level OR per-ENI
#           Association.PublicIp) unless instance carries the
#           "internet-facing" tag (Tags list entry with Key=
#           "internet-facing").
# ---------------------------------------------------------------------

package main

# Acknowledgement tag short-circuits the violation.
internet_tagged_aws {
    input.Tags[_].Key == "internet-facing"
}

# Either top-level PublicIpAddress OR any ENI's Association.PublicIp.
has_public_ip_top_level {
    input.PublicIpAddress
    input.PublicIpAddress != ""
}

has_public_ip_eni[i] {
    input.NetworkInterfaces[i].Association.PublicIp
    input.NetworkInterfaces[i].Association.PublicIp != ""
}

deny[msg] {
    has_public_ip_top_level
    not internet_tagged_aws
    msg := sprintf(
        "[HIGH][ec2_no_public_ip] instance %s has a public IPv4 address (%s) without the 'internet-facing' tag",
        [input.name, input.PublicIpAddress],
    )
}

deny[msg] {
    count(has_public_ip_eni) > 0
    not has_public_ip_top_level
    not internet_tagged_aws
    msg := sprintf(
        "[HIGH][ec2_no_public_ip] instance %s has public IPs on %d network interface(s) without the 'internet-facing' tag",
        [input.name, count(has_public_ip_eni)],
    )
}
