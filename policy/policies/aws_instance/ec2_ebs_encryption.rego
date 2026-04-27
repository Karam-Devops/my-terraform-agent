# ec2_ebs_encryption.rego
#
# Every EBS volume attached to an EC2 instance MUST be encrypted.
# Unencrypted EBS volumes are failed-audit material on every regulated
# workload (PCI DSS 3.5, HIPAA Security Rule, SOC2 CC6.1) -- regardless
# of who manages the key. Customer-managed KMS keys (CMEK) are required
# for the strictest controls, but for this baseline rule we accept any
# encryption (KMS-managed OR customer-managed) -- the failure mode is
# "volume is plaintext at the disk layer".
#
# This is the AWS analogue of GCP's `gce_disk_encryption` rule.
#
# Snapshot fields (aws ec2 describe-instances + describe-volumes output):
#   BlockDeviceMappings[].Ebs.VolumeId       (volume identifier)
#   BlockDeviceMappings[].Ebs.Encrypted      (bool, encrypt at rest)
#   BlockDeviceMappings[].DeviceName         (disk identifier for the message)
#
# Note: aws ec2 describe-instances returns the BlockDeviceMappings
# WITHOUT the Encrypted flag -- you must cross-reference describe-volumes
# to get it. Snapshot fetcher (future Phase 4-5 AWS support) is expected
# to populate `Ebs.Encrypted` inline by joining the two API calls. This
# rule assumes the joined shape.
#
# Severity: HIGH -- audit-failing on every workload subject to
# data-residency or data-protection regulations.

package main

# Set of indices for unencrypted EBS volumes attached to this instance.
unencrypted_ebs[i] {
    bdm := input.BlockDeviceMappings[i]
    bdm.Ebs
    not bdm.Ebs.Encrypted
}

deny[msg] {
    count(unencrypted_ebs) > 0
    # Surface the FIRST unencrypted device's name so the message points
    # at a specific volume the operator can fix. Pick by lowest index
    # (deterministic across runs).
    sorted := sort(unencrypted_ebs)
    first_idx := sorted[0]
    device_name := input.BlockDeviceMappings[first_idx].DeviceName
    msg := sprintf(
        "[HIGH][ec2_ebs_encryption] instance %s has unencrypted EBS volume on device '%s' (Ebs.Encrypted must be true)",
        [input.name, device_name],
    )
}
