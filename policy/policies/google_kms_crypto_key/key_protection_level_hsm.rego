# key_protection_level_hsm.rego
# Source: GCP policy-library (archived 2025-08-20) gcp_cmek_settings_v1
# Standard: Industry consensus (FIPS 140-2 L3 for regulated workloads) | NIST SP 800-53 SC-12, SC-13
# Default: Require versionTemplate.protectionLevel == "HSM" (Google's archive parameterizes; we hardcode HSM as the floor for any key in regulated scope)
# See docs/policy_provenance.md for full mining details.

package main

deny[msg] {
    vt := object.get(input, "versionTemplate", {})
    pl := object.get(vt, "protectionLevel", "SOFTWARE")
    pl != "HSM"
    msg := sprintf(
        "[MED][key_protection_level_hsm] KMS key %s uses protectionLevel '%v' (must be HSM for regulated workloads; SOFTWARE keys live in process memory, HSM keys live in FIPS 140-2 L3 hardware)",
        [input.name, pl],
    )
}
