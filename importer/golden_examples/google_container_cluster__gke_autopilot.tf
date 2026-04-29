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

  # ip_allocation_policy: TWO mutually-exclusive patterns. PICK ONE,
  # NEVER MIX. The Google provider rejects mixed pairs with:
  #   "Error: Conflicting configuration arguments
  #    cluster_secondary_range_name: conflicts with services_ipv4_cidr_block"
  #
  # Pattern A (CIDR-based, USED HERE -- recommended for fresh clusters):
  ip_allocation_policy {
    cluster_ipv4_cidr_block  = "/14"
    services_ipv4_cidr_block = "/20"
  }
  #
  # Pattern B (NAMED-secondary-ranges, for clusters that reference
  # pre-existing secondary ranges on the subnetwork). When the cloud
  # snapshot has `cluster_secondary_range_name` populated, use BOTH
  # name fields together -- DO NOT mix one name with one CIDR:
  #   ip_allocation_policy {
  #     cluster_secondary_range_name  = "gke-poc-cluster-pods-XXXX"
  #     services_secondary_range_name = "gke-poc-cluster-services-XXXX"
  #   }
  # If the snapshot has BOTH name and CIDR fields, prefer the name
  # variant -- it preserves the operator's pre-existing subnet layout.

  release_channel {
    channel = "REGULAR"
  }

  # Workload Identity -- the recommended pattern; Autopilot creates
  # the workload pool by default but explicit declaration is
  # idiomatic.
  workload_identity_config {
    workload_pool = "example-project.svc.id.goog"
  }

  # logging_config and monitoring_config: CRITICAL schema note
  # (P4-9c, 2026-04-29 cluster smoke). The cloud snapshot's API JSON
  # nests the components inside a `componentConfig` wrapper:
  #   "monitoringConfig": {
  #     "componentConfig": {
  #       "enableComponents": ["SYSTEM_COMPONENTS", ...]
  #     }
  #   }
  # The TF provider v6+ schema FLATTENED this -- `enable_components`
  # lives DIRECTLY inside monitoring_config / logging_config, NOT
  # inside a component_config sub-block. Writing the v1-style nested
  # wrapper produces TWO terraform errors:
  #   * "argument enable_components is required" (parent has none)
  #   * "Blocks of type component_config are not expected here"
  # mtagent's snapshot_scrubber._PER_TYPE_UNWRAPS pre-flattens the
  # snapshot so the LLM sees the correct shape -- this golden example
  # reinforces it as the canonical form.
  logging_config {
    enable_components = ["SYSTEM_COMPONENTS", "WORKLOADS"]
  }

  monitoring_config {
    enable_components = ["SYSTEM_COMPONENTS"]
    # managed_prometheus is a sibling block inside monitoring_config,
    # NOT a top-level resource attribute. The cloud snapshot's
    # `managedPrometheusConfig` (note _Config suffix on the API name)
    # gets stripped post-LLM by post_llm_overrides.json's
    # block_deletions rule -- the CORRECT block name is just
    # `managed_prometheus` (no _config suffix).
    managed_prometheus {
      enabled = true
    }
  }

  # ignore_changes for fields the GCP control plane recomputes on
  # cluster maintenance windows / upgrades. NOTE: identifiers are
  # bare (no quotes). Quoted strings here ("min_master_version")
  # are Terraform 0.11 syntax; Terraform 1.x emits a deprecation
  # warning that may become an error in future major versions.
  lifecycle {
    ignore_changes = [
      node_version,
      min_master_version,
    ]
  }
}
