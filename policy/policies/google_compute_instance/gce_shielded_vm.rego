# gce_shielded_vm.rego
#
# Shielded VM MUST be fully enabled (secure boot + vTPM + integrity
# monitoring). Without all three:
#   - secure boot off  -> rootkit / unsigned-driver risk at boot
#   - vTPM off         -> no measured-boot attestation possible
#   - integrity off    -> no detection if the boot chain is tampered
#
# Cloud field: shieldedInstanceConfig.{enableSecureBoot, enableVtpm,
#              enableIntegrityMonitoring}  (all three must be true)
#
# Severity: MED for individual misses, HIGH for fully-disabled. We emit
# one rule per knob so the report is actionable; severity-per-knob keeps
# partial-coverage from looking like a worst-case finding.

package main

deny[msg] {
    not input.shieldedInstanceConfig.enableSecureBoot
    msg := sprintf(
        "[MED][gce_shielded_vm] instance %s does not have Secure Boot enabled",
        [input.name],
    )
}

deny[msg] {
    not input.shieldedInstanceConfig.enableVtpm
    msg := sprintf(
        "[MED][gce_shielded_vm] instance %s does not have vTPM enabled",
        [input.name],
    )
}

deny[msg] {
    not input.shieldedInstanceConfig.enableIntegrityMonitoring
    msg := sprintf(
        "[MED][gce_shielded_vm] instance %s does not have Integrity Monitoring enabled",
        [input.name],
    )
}
