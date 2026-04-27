# Golden example: Compute Subnetwork (CC-9 P4-9b)
#
# CRITICAL hallucination-class fields to OMIT:
#   * NO purpose field unless intentionally creating a proxy-only or
#     private-NAT subnetwork (default purpose is fine for general use;
#     the subnet_flow_logs_enabled.rego rule exempts proxy purposes).
#   * NO secondary_ip_range as a top-level dict -- it's a repeated
#     block; can have multiple entries.
#
# Required: name, ip_cidr_range, region, network.
# Recommended (policy-rule-required):
#   * log_config.enable = true (subnet_flow_logs_enabled.rego)
#   * private_ip_google_access = true (subnet_private_google_access.rego)

resource "google_compute_subnetwork" "subnet_example" {
  name          = "poc-subnet-example"
  ip_cidr_range = "10.10.0.0/20"
  region        = "us-central1"
  network       = google_compute_network.vpc_example.id

  # Required by subnet_private_google_access.rego: VMs without
  # external IPs need this to reach Google APIs without traversing
  # public internet.
  private_ip_google_access = true

  # Required by subnet_flow_logs_enabled.rego: VPC flow logs for
  # network audit trail (CIS GCP 3.8).
  log_config {
    aggregation_interval = "INTERVAL_5_SEC"
    flow_sampling        = 0.5
    metadata             = "INCLUDE_ALL_METADATA"
  }

  # Secondary ranges for GKE -- pods + services. Required when
  # ip_allocation_policy on a cluster references this subnet
  # (the canonical pattern for VPC-native clusters).
  secondary_ip_range {
    range_name    = "pods"
    ip_cidr_range = "10.20.0.0/14"
  }
  secondary_ip_range {
    range_name    = "services"
    ip_cidr_range = "10.24.0.0/20"
  }
}
