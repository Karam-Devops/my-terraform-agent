# Golden example: GKE Standard cluster (CC-9 P4-9a)
#
# Demonstrates the canonical Standard-mode shape. KEY DIFFERENCES
# from Autopilot (do NOT include either of these on Standard):
#   * NO enable_autopilot = true.
#   * NO Autopilot-managed addon configurations.
#
# Required: at least one node_pool (or remove_default_node_pool +
#   separate google_container_node_pool resources -- the
#   "decoupled node pool" pattern that lets you upgrade pools
#   independently of cluster).
# Required: ip_allocation_policy for VPC-native (default since
#   GKE 1.21).
#
# CRITICAL value-type notes (P2-14): GKE returns several enums as
# QUOTED STRINGS, not booleans. e.g.
#   insecure_kubelet_readonly_port_enabled = "FALSE"   # NOT false
# When mapping cloud->HCL, prefer quoted enums over bare booleans
# for any field whose schema declares string type.

resource "google_container_cluster" "standard_example" {
  name     = "poc-cluster-standard"
  location = "us-central1"

  # The "remove default + manage pools separately" pattern is
  # idiomatic for prod -- lets you upgrade pools without re-creating
  # the cluster.
  remove_default_node_pool = true
  initial_node_count       = 1

  network    = "projects/example-project/global/networks/default"
  subnetwork = "projects/example-project/regions/us-central1/subnetworks/default"

  ip_allocation_policy {
    cluster_ipv4_cidr_block  = "/14"
    services_ipv4_cidr_block = "/20"
  }

  release_channel {
    channel = "REGULAR"
  }

  workload_identity_config {
    workload_pool = "example-project.svc.id.goog"
  }

  # Master authorized networks -- valid on Standard (Autopilot
  # manages this internally).
  master_authorized_networks_config {
    cidr_blocks {
      cidr_block   = "10.0.0.0/8"
      display_name = "internal-only"
    }
  }

  lifecycle {
    ignore_changes = [
      node_version,
      min_master_version,
      initial_node_count,
    ]
  }
}
