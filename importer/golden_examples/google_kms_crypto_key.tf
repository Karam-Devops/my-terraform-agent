# Golden example: KMS Crypto Key (CC-9 P4-9b)
#
# CRITICAL hallucination-class fields to OMIT:
#   * NO `name = "<full URN>"` -- the name field is the SHORT name
#     ("poc-key"), not the URN ("projects/.../keyRings/k/cryptoKeys/poc-key").
#     CC-8 P2-6 fixed the importer side; the LLM sometimes echoes the URN
#     anyway. Use the short name.
#   * NO purpose = "ENCRYPT_DECRYPT" if you want to use the GCS
#     encryption integration -- "ENCRYPT_DECRYPT" is the default and
#     omitting it is cleaner.
#
# Required: name, key_ring (parent reference).
# Recommended (policy-rule-required):
#   * rotation_period <= 7776000s (90 days) -- our
#     key_rotation_max_90_days.rego rule. Stricter than Google's
#     archived 1-year default.
#   * version_template.protection_level = "HSM" -- our
#     key_protection_level_hsm.rego rule (FIPS 140-2 L3).

resource "google_kms_crypto_key" "key_example" {
  name     = "poc-bucket-key"
  key_ring = google_kms_key_ring.keyring_example.id

  # Rotation period: 90 days = 90 * 86400 = 7776000 seconds.
  # Stricter than Google's archived 1-year default; matches CIS GCP 1.10.
  rotation_period = "7776000s"

  # HSM-backed: required by key_protection_level_hsm.rego.
  # SOFTWARE keys live in process memory; HSM keys live in
  # FIPS 140-2 L3 hardware modules.
  version_template {
    algorithm        = "GOOGLE_SYMMETRIC_ENCRYPTION"
    protection_level = "HSM"
  }

  # Prevent accidental destruction -- crypto keys with active data
  # encrypt to dependent resources are catastrophic to lose.
  lifecycle {
    prevent_destroy = true
  }
}
