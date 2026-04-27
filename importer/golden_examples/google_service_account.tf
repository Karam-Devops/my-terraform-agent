# Golden example: IAM Service Account (CC-9 P4-9b)
#
# CRITICAL hallucination-class fields to OMIT:
#   * NO `name = "projects/.../serviceAccounts/...@..."` -- the name
#     field is the SHORT account_id ("poc-sa"), the full URN is
#     computed in the email field.
#   * NO project_id field -- it's `project` (not project_id).
#   * NO oauth_client_id / oauth2_client_id -- both are computed.
#
# Required: account_id (the short name; provider builds the email).
# Recommended: display_name (human-readable), description (operational
#   hygiene -- the description should explain what this SA is for so
#   future operators understand whether it's safe to delete).

resource "google_service_account" "sa_example" {
  account_id   = "poc-cloudrun-sa"
  display_name = "POC Cloud Run service account"
  description  = "Service account for the POC Cloud Run v2 service. Permissions: storage object reader (poc-bucket-example), pubsub publisher (poc-topic-example). Owner: platform team."
  project      = "example-project"

  # Disabled is the explicit kill-switch. Default false. Setting it
  # to true is a way to revoke an SA's access without deleting it
  # (preserves history; reversible).
  disabled = false
}

# Sibling pattern (NOT in this single resource block): grant the SA
# the IAM roles it needs via google_project_iam_member -- NEVER
# google_project_iam_binding (which is authoritative and would
# reset other members). Example below in commented form for
# reference; not part of the actual resource:
#
# resource "google_project_iam_member" "sa_storage_reader" {
#   project = "example-project"
#   role    = "roles/storage.objectViewer"
#   member  = "serviceAccount:${google_service_account.sa_example.email}"
# }
