# gce_disk_encryption.rego
#
# Every disk attached to the instance MUST be encrypted with a customer-
# managed key (CMEK). Same audit rationale as bucket_encryption.rego —
# Google-managed encryption is the default but does not satisfy regulated
# controls.
#
# Cloud field: disks[].diskEncryptionKey.kmsKeyName  (must be set on every disk)
#
# Severity: HIGH — failed-audit material on every regulated workload.
# We emit one violation per misconfigured disk so the report says exactly
# which disk to fix instead of forcing the operator to grep through the
# raw snapshot.

package main

# Disks that lack a customer-managed encryption key.
unencrypted_disk_indices[i] {
    disk := input.disks[i]
    not disk.diskEncryptionKey.kmsKeyName
}

deny[msg] {
    i := unencrypted_disk_indices[_]
    disk := input.disks[i]
    # Surface deviceName when present (humans recognize disks by name, not
    # index); fall back to the array index when the snapshot omits it.
    disk_label := disk_display_name(disk, i)
    msg := sprintf(
        "[HIGH][gce_disk_encryption] instance %s has disk '%s' without a customer-managed encryption key (diskEncryptionKey.kmsKeyName)",
        [input.name, disk_label],
    )
}

disk_display_name(disk, i) := name {
    name := disk.deviceName
} else := name {
    name := sprintf("disks[%d]", [i])
}
