# Golden example: GKE Autopilot cluster (CC-9 P4-9a)
#
# Demonstrates the canonical Autopilot-mode shape. KEY DIFFERENCES
# from a Standard cluster (do NOT include any of these on Autopilot):
#   * NO node_pool blocks (Autopilot manages node pools internally).
#   * NO addons_config.ray_operator_config (P2-13: Autopilot rejects).
#   * NO addons_config.network_policy_config (Autopilot manages).
#   * NO master_authorized_networks (Autopilot manages -- only
#     applicable in Standard mode).
#   * NO advanced_datapath_observability_config (P2-9.1: Autopilot
#     rejects).
#   * NO insecure_kubelet_readonly_port_enabled (Autopilot manages).
#
# Required: enable_autopilot = true.
# Required: ip_allocation_policy block (Autopilot is always VPC-native).

resource "google_container_cluster" "autopilot_example" {
  name     = "poc-cluster-autopilot"
  location = "us-central1"

  enable_autopilot = true

  network    = "projects/example-project/global/networks/default"
  subnetwork = "projects/example-project/regions/us-central1/subnetworks/default"

  ip_allocation_policy {
    cluster_ipv4_cidr_block  = "/14"
    services_ipv4_cidr_block = "/20"
  }

  release_channel {
    channel = "REGULAR"
  }

  # Workload Identity -- the recommended pattern; Autopilot creates
  # the workload pool by default but explicit declaration is
  # idiomatic.
  workload_identity_config {
    workload_pool = "example-project.svc.id.goog"
  }

  # ignore_changes for fields the GCP control plane recomputes on
  # cluster maintenance windows / upgrades.
  lifecycle {
    ignore_changes = [
      node_version,
      min_master_version,
    ]
  }
}
