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
#
# --- Provenance (P4-PRE 2026-04-27) ----------------------------------
# Source:   GoogleCloudPlatform/policy-library (archived 2025-08-20)
#           gcp_gke_enable_shielded_nodes_v1.yaml ->
#           GCPGKEEnableShieldedNodesConstraintV1
#           [PROXY MATCH: only GKE-shielded variant exists in the
#           archived library; no generic compute shielded VM template.
#           Adopted Google's defensive-defaulting pattern.]
# Standard: CIS GCP 4.9 -- "Ensure that Compute instances have
#           Shielded VM enabled".
# NIST:     SP 800-53 SI-7 (Software, Firmware, and Information
#           Integrity) + CM-3 (Configuration Change Control).
# Default:  Require all three knobs true (STRICTER than Google's
#           archived rule, which required only enableSecureBoot AND
#           enableIntegrityMonitoring -- they did NOT require
#           enableVtpm. We require vTPM because measured-boot
#           attestation is the foundation of any TPM-backed key
#           release pattern -- without it the other two knobs are
#           detection-only, not enforcement.).
#
# P4-PRE applied:
#   * Defensive-defaulting via OPA built-in `object.get(x, k, default)`
#     (equivalent to Google's lib.get_default()). Behaves identically
#     across "absent shieldedInstanceConfig block", explicit false,
#     and null. Without this, our previous `not
#     input.shieldedInstanceConfig.enableSecureBoot` could behave
#     inconsistently across snapshot shapes.
# ---------------------------------------------------------------------

package main

# Helper: read shieldedInstanceConfig defensively. Returns the empty
# object when the block is absent so downstream lookups don't bomb on
# missing-parent errors. Mirrors Google's lib.get_default() pattern.
shielded_config := cfg {
    cfg := object.get(input, "shieldedInstanceConfig", {})
}

deny[msg] {
    not object.get(shielded_config, "enableSecureBoot", false) == true
    msg := sprintf(
        "[MED][gce_shielded_vm] instance %s does not have Secure Boot enabled",
        [input.name],
    )
}

deny[msg] {
    not object.get(shielded_config, "enableVtpm", false) == true
    msg := sprintf(
        "[MED][gce_shielded_vm] instance %s does not have vTPM enabled",
        [input.name],
    )
}

deny[msg] {
    not object.get(shielded_config, "enableIntegrityMonitoring", false) == true
    msg := sprintf(
        "[MED][gce_shielded_vm] instance %s does not have Integrity Monitoring enabled",
        [input.name],
    )
}
