# Golden example: Cloud Storage bucket (CC-9 P4-9b)
#
# CRITICAL hallucination-class fields to OMIT:
#   * NO project = "..." -- inferred from provider config; explicit
#     project on storage buckets is redundant + sometimes rejected.
#   * NO storage_url / url -- both are computed outputs, not inputs.
#   * NO force_destroy on prod buckets -- behavioral flag never sent
#     to cloud, often left as default.
#
# Required: name, location.
# Recommended (and policy-rule-required): uniform_bucket_level_access
#   = true (bucket_public_access.rego), versioning.enabled = true
#   (bucket_versioning.rego), encryption with CMEK
#   (bucket_encryption.rego), soft_delete_policy >= 7 days
#   (bucket_retention.rego).

resource "google_storage_bucket" "bucket_example" {
  name          = "poc-bucket-example"
  location      = "US-CENTRAL1"
  storage_class = "STANDARD"

  # Uniform bucket-level access: required by bucket_public_access.rego.
  # Disables legacy ACLs entirely.
  uniform_bucket_level_access = true

  # Public Access Prevention: stronger than UBLA alone -- denies any
  # ACL or IAM binding that would grant public access.
  public_access_prevention = "enforced"

  # Versioning: required by bucket_versioning.rego. Without it a
  # single delete is unrecoverable.
  versioning {
    enabled = true
  }

  # CMEK: required by bucket_encryption.rego.
  encryption {
    default_kms_key_name = "projects/example-project/locations/us-central1/keyRings/poc-keyring/cryptoKeys/poc-bucket-key"
  }

  # Soft-delete retention: required by bucket_retention.rego (>= 7d).
  # 604800 seconds = 7 days exactly (the GCS minimum).
  soft_delete_policy {
    retention_duration_seconds = 604800
  }

  # Object lifecycle -- transition to colder storage after 90d.
  lifecycle_rule {
    condition {
      age = 90
    }
    action {
      type          = "SetStorageClass"
      storage_class = "NEARLINE"
    }
  }

  labels = {
    team = "platform"
    env  = "prod"
  }
}
