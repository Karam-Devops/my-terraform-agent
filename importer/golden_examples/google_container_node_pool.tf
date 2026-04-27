# Golden example: GKE node pool (CC-9 P4-9b)
#
# The "decoupled node pool" pattern -- separate google_container_node_pool
# resource attached to a cluster that has remove_default_node_pool = true.
# This lets you upgrade pools independently of cluster.
#
# CRITICAL hallucination-class fields to OMIT:
#   * NO autoscaling block when initial_node_count is fixed (mutually
#     exclusive in many provider versions; pick one).
#   * Per-pool service account in node_config (NOT cluster.service_account
#     -- different field).
#
# Required: cluster reference, node_config block.
# Recommended: management.{auto_repair, auto_upgrade} -- our policy
#   rules enforce both.

resource "google_container_node_pool" "primary_example" {
  name     = "poc-pool-primary"
  cluster  = google_container_cluster.standard_example.id
  location = "us-central1"

  node_count = 2

  # Auto-upgrade + auto-repair: our P4-6 policy rules
  # node_pool_auto_upgrade.rego + node_pool_auto_repair.rego enforce
  # these. Setting them in the example reinforces the right pattern.
  management {
    auto_repair  = true
    auto_upgrade = true
  }

  node_config {
    machine_type = "e2-standard-4"
    # COS_CONTAINERD: required by our policy rule
    # node_pool_uses_cos.rego (P4-6).
    image_type = "COS_CONTAINERD"

    # DEDICATED service account (NOT 'default'): required by our
    # policy rule node_pool_no_default_sa.rego (P4-6).
    service_account = "poc-gke-nodes@example-project.iam.gserviceaccount.com"
    oauth_scopes = [
      "https://www.googleapis.com/auth/cloud-platform",
    ]

    # Workload Identity binding -- the modern pattern for pod auth.
    workload_metadata_config {
      mode = "GKE_METADATA"
    }

    # Shielded VM defaults -- secure boot + integrity monitoring.
    shielded_instance_config {
      enable_secure_boot          = true
      enable_integrity_monitoring = true
    }
  }

  lifecycle {
    ignore_changes = [
      node_count,
      version,
    ]
  }
}
