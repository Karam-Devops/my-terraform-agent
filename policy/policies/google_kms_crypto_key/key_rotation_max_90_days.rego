# key_rotation_max_90_days.rego
# Source: GCP policy-library (archived 2025-08-20) gcp_cmek_rotation_v1 (default: 31536000s/1y; we choose 90d per CIS)
# Standard: CIS GCP 1.10 / 4.8 (KMS rotation <= 90 days) | NIST SP 800-53 SC-12 (Cryptographic Key Establishment)
# Default: Require rotationPeriod <= 90 days = 7776000s (STRICTER than Google's archive default of 1 year). Sentinel "99999999s" mined from Google's template = "never rotates" -> always deny.
# See docs/policy_provenance.md for full mining details.

package main

# Maximum allowed rotation period in seconds. 90 days = 90 * 24 * 3600.
# Sourced from CIS GCP 1.10 (NOT from Google's archived default of
# 31536000s = 1 year, which is too lax for regulated workloads).
max_rotation_seconds := 7776000

# Helper: parse the "<N>s" duration string to integer seconds. Google's
# sentinel for "never rotates" is "99999999s" -- by parsing this as a
# huge number, the inequality below trips automatically (no special
# case needed).
rotation_seconds := s {
    rp := object.get(input, "rotationPeriod", "99999999s")
    # Strip trailing "s" suffix.
    s := to_number(trim_suffix(rp, "s"))
}

deny[msg] {
    rotation_seconds > max_rotation_seconds
    msg := sprintf(
        "[HIGH][key_rotation_max_90_days] KMS key %s has rotationPeriod %vs (must be <= 7776000s = 90 days; CIS GCP 1.10). Google's archived default was 31536000s (1 year); we choose stricter.",
        [input.name, rotation_seconds],
    )
}
