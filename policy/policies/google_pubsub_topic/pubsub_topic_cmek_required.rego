# pubsub_topic_cmek_required.rego
# Source: NONE (Pub/Sub not covered in GoogleCloudPlatform/policy-library archived library)
# Standard: CIS Controls v8 3.11 (Encrypt Sensitive Data at Rest) | NIST SP 800-53 SC-28
# Default: Require kmsKeyName non-empty (Pub/Sub message payloads encrypted with customer-managed key, not Google-managed default)
# Industry source: Google Cloud "Pub/Sub message encryption" docs.
# See docs/policy_provenance.md for full mining details.

package main

deny[msg] {
    name := object.get(input, "kmsKeyName", "")
    name == ""
    msg := sprintf(
        "[HIGH][pubsub_topic_cmek_required] Pub/Sub topic %s has no customer-managed encryption key (kmsKeyName must be set) -- regulated workloads require CMEK over Google-managed default (CIS Controls v8 3.11)",
        [input.name],
    )
}
