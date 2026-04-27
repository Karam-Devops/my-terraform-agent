# pubsub_sub_dead_letter_configured.rego
# Source: NONE (Pub/Sub not covered in GoogleCloudPlatform/policy-library archived library)
# Standard: Industry consensus (operational resilience) | NIST SP 800-53 SI-11 (Error Handling)
# Default: Require deadLetterPolicy.deadLetterTopic non-empty (poison messages route to DLQ instead of retrying forever)
# See docs/policy_provenance.md for full mining details.

package main

deny[msg] {
    dlp := object.get(input, "deadLetterPolicy", {})
    topic := object.get(dlp, "deadLetterTopic", "")
    topic == ""
    msg := sprintf(
        "[MED][pubsub_sub_dead_letter_configured] Pub/Sub subscription %s has no dead-letter topic configured (deadLetterPolicy.deadLetterTopic must be set) -- poison messages will retry indefinitely, blocking the subscriber",
        [input.name],
    )
}
