# Golden example: Cloud Run v2 Service (CC-9 P4-9a; trimmed P4-11)
#
# Canonical v2 shape -- mirror what's BELOW.
#
# P4-11 lesson: forbidden v1-vestige fields (startup_cpu_boost,
# container_concurrency, latest_revision) used to be enumerated
# in prose comments here. SMOKE 4 proved comments-as-negative-
# signal don't override input data -- the LLM still echoed
# startup_cpu_boost from the cloud snapshot. Comments removed;
# their ABSENCE from the example below is the signal. Belt-and-
# braces: importer.resource_mode.cloud_run_v2_default mode strips
# these fields from the snapshot pre-LLM, and post_llm_overrides.json
# strips them post-LLM if the LLM still emits them.
#
# Required v2 shape:
#   template.containers[].image
#   template.scaling.{min_instance_count, max_instance_count}
#     (also satisfies the cloudrun_min_instances_documented policy
#     rule from P4-7).
#
# v2 traffic uses string-typed allocation:
#   traffic { type = "TRAFFIC_TARGET_ALLOCATION_TYPE_LATEST" }
# Health probes (startup_probe, liveness_probe) live INSIDE
# template.containers[], NOT at template-level.

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

  traffic {
    type    = "TRAFFIC_TARGET_ALLOCATION_TYPE_LATEST"
    percent = 100
  }
}
