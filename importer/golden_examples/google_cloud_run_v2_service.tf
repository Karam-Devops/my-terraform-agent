# Golden example: Cloud Run v2 Service (CC-9 P4-9a)
#
# Demonstrates the canonical v2 shape. KEY DIFFERENCES from the v1
# google_cloud_run_service resource (do NOT include any of these on
# v2 -- they're v1 vestiges that the v2 provider rejects):
#   * NO container_concurrency at the top level (v1 placement;
#     v2 uses template.scaling.max_instance_count + max_instance_request_concurrency).
#   * NO latest_revision = true (v1-only routing field).
#   * NO startup_cpu_boost at template level (P2-12: v2 relocated
#     this to template.containers.startup_probe and renamed; safer
#     to omit entirely until needed).
#   * NO traffic block with `latest_revision = true` (use
#     traffic { type = "TRAFFIC_TARGET_ALLOCATION_TYPE_LATEST" }).
#
# Required v2 shape:
#   template.containers[].image
#   template.scaling.{min_instance_count, max_instance_count} (CC-9
#     also enforces explicit min via cloudrun_min_instances_documented
#     policy rule -- declaring it satisfies that rule).
#
# CRITICAL: v2 services use string type identifiers (e.g.
# "TRAFFIC_TARGET_ALLOCATION_TYPE_LATEST") not v1's bare enums.

resource "google_cloud_run_v2_service" "service_example" {
  name     = "poc-cloudrun-v2"
  location = "us-central1"
  project  = "example-project"

  ingress = "INGRESS_TRAFFIC_INTERNAL_LOAD_BALANCER"

  template {
    # Explicit scaling -- satisfies cloudrun_min_instances_documented
    # rule; min=0 is fine if cost-optimized cold-start is acceptable.
    scaling {
      min_instance_count = 0
      max_instance_count = 10
    }

    # Service account -- explicit dedicated SA (NOT default Compute).
    service_account = "poc-cloudrun-sa@example-project.iam.gserviceaccount.com"

    containers {
      image = "us-central1-docker.pkg.dev/example-project/repo/api:latest"

      ports {
        container_port = 8080
      }

      resources {
        limits = {
          cpu    = "1000m"
          memory = "512Mi"
        }
      }

      # Health probe -- v2 nests startup_probe (and liveness_probe)
      # INSIDE containers[], not at template level (v1's placement).
      startup_probe {
        initial_delay_seconds = 5
        period_seconds        = 10
        timeout_seconds       = 5
        failure_threshold     = 3
        tcp_socket {
          port = 8080
        }
      }
    }
  }

  # v2 traffic block uses string type, NOT v1's latest_revision = true.
  traffic {
    type    = "TRAFFIC_TARGET_ALLOCATION_TYPE_LATEST"
    percent = 100
  }
}
