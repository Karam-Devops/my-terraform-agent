# policy/config.py
"""
Policy enforcer scope, severity model, and policy-bundle layout.

Resource-type-to-policy mapping is implicit in the directory layout:

    policy/policies/
    ├── common/                         # applies to ALL in-scope types
    │   └── mandatory_labels.rego
    ├── google_compute_instance/        # applies to GCE only
    │   ├── gce_no_public_ip.rego
    │   ├── gce_shielded_vm.rego
    │   └── gce_disk_encryption.rego
    └── google_storage_bucket/          # applies to buckets only
        ├── bucket_encryption.rego
        ├── bucket_public_access.rego
        ├── bucket_versioning.rego
        └── bucket_retention.rego

Filename convention: `<rule_id>.rego`. The engine resolves a violation's
source file by looking up `<rule_id>.rego` in the dirs that were evaluated.
This keeps the engine zero-config and the report human-readable.
"""

import os

# --- Scope -----------------------------------------------------------------

# Resource types we evaluate. Mirrors detector/config.py IN_SCOPE_TF_TYPES;
# kept independent so policy can be enabled per-type without coupling.
IN_SCOPE_TF_TYPES = {
    "google_compute_instance",
    "google_storage_bucket",
}

# --- Policy bundle layout --------------------------------------------------

POLICY_DIR = os.path.join(os.path.dirname(__file__), "policies")
COMMON_POLICY_DIR = os.path.join(POLICY_DIR, "common")


def policies_dir_for(tf_type: str) -> str:
    """Returns the per-type policy directory. Missing dirs are fine —
    engine.evaluate() silently skips dirs that don't exist or contain no
    .rego files, so adding a new resource type to IN_SCOPE_TF_TYPES doesn't
    require creating a directory upfront."""
    return os.path.join(POLICY_DIR, tf_type)


# --- Severity model --------------------------------------------------------

# Numeric weights drive sort order (high → low) and the CI-fail threshold.
SEVERITY_WEIGHTS = {
    "HIGH": 30,
    "MED":  20,
    "LOW":  10,
}

# Standalone CLI exits non-zero when any violation at or above this
# severity is seen. MED+ would be too noisy for a POC; HIGH is the right
# enterprise default ("things that would fail an audit").
FAIL_AT_SEVERITY = "HIGH"
