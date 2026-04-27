# node_pool_uses_cos.rego
# Source: GCP policy-library (archived 2025-08-20) gcp_gke_container_optimized_os
# Standard: Industry consensus (minimal attack surface) | NIST SP 800-53 CM-7 (Least Functionality)
# Default: Require config.imageType in {"COS", "COS_CONTAINERD"} (Container-Optimized OS = read-only root, no package manager, no extras)
# See docs/policy_provenance.md for full mining details.

package main

# Set of allowed imageType values mined from Google's template.
# COS = Container-Optimized OS (legacy docker runtime).
# COS_CONTAINERD = COS with containerd runtime (modern; required GKE 1.24+).
allowed_image_types := {"COS", "COS_CONTAINERD"}

deny[msg] {
    cfg := object.get(input, "config", {})
    image_type := object.get(cfg, "imageType", "")
    not allowed_image_types[image_type]
    msg := sprintf(
        "[MED][node_pool_uses_cos] node pool %s uses imageType '%v' (must be COS or COS_CONTAINERD for minimal attack surface)",
        [input.name, image_type],
    )
}
