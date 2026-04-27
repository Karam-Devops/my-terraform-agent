# Golden example: Pub/Sub Subscription (CC-9 P4-9b)
#
# CRITICAL hallucination-class fields to OMIT:
#   * NO `name = "<full URN>"` -- same CC-8 P2-6 pattern as KMS keys
#     and Pub/Sub topics. Use short name; provider builds the URN.
#   * NO push_config block when using pull mode (mutually exclusive).
#   * NO ack_deadline_seconds outside [10, 600] -- cloud rejects.
#
# Required: name, topic (parent reference).
# Recommended (policy-rule-required):
#   * dead_letter_policy.dead_letter_topic -- our
#     pubsub_sub_dead_letter_configured.rego rule.
#
# CRITICAL: subscription has its own IAM resource
# (google_pubsub_subscription_iam_*) -- our pubsub_sub_iam_no_allusers
# rule evaluates the IAM policy attached to the subscription, NOT
# bindings declared inline on the subscription resource (Pub/Sub
# subscriptions don't carry inline IAM bindings).

resource "google_pubsub_subscription" "subscription_example" {
  name  = "poc-subscription-example"
  topic = google_pubsub_topic.topic_example.id

  # Pull mode (most common). Push mode would set push_config and OMIT
  # ack_deadline_seconds (push uses HTTP response codes for ack).
  ack_deadline_seconds = 30

  # Message retention: how long unacked messages stay in the queue.
  # Default is 7 days (604800s); explicit declaration documents intent.
  message_retention_duration = "604800s"

  # Dead-letter policy: REQUIRED by pubsub_sub_dead_letter_configured.rego.
  # Poison messages route to the DLQ topic instead of retrying forever.
  dead_letter_policy {
    dead_letter_topic     = google_pubsub_topic.dlq_topic.id
    max_delivery_attempts = 5
  }

  # Exponential backoff on redelivery for transient subscriber errors.
  retry_policy {
    minimum_backoff = "10s"
    maximum_backoff = "600s"
  }

  # Filter -- subscriber only sees messages matching this attribute
  # selector. Empty filter (default) means all messages.
  filter = "attributes.priority = \"high\""

  labels = {
    team = "platform"
    env  = "prod"
  }
}
