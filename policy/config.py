# policy/config.py
"""
Policy enforcer scope, severity model, and policy-bundle layout.

Resource-type-to-policy mapping is implicit in the directory layout:

    policy/policies/
    ├── common/                         # applies to ALL in-scope types
    │   ├── mandatory_labels.rego       (GCP shape: input.labels.X)
    │   └── mandatory_tags.rego         (AWS shape: input.Tags[].Key)
    │
    ├── google_compute_instance/        # GCE only
    │   ├── gce_no_public_ip.rego
    │   ├── gce_shielded_vm.rego
    │   └── gce_disk_encryption.rego
    ├── google_storage_bucket/          # GCS bucket only
    │   ├── bucket_encryption.rego
    │   ├── bucket_public_access.rego
    │   ├── bucket_versioning.rego
    │   └── bucket_retention.rego
    │
    ├── aws_instance/                   # EC2 only (P3-4)
    │   ├── ec2_no_public_ip.rego
    │   ├── ec2_imds_v2.rego
    │   └── ec2_ebs_encryption.rego
    └── aws_s3_bucket/                  # S3 bucket only (P3-4)
        ├── s3_bucket_encryption.rego
        ├── s3_bucket_public_access.rego
        ├── s3_bucket_versioning.rego
        └── s3_bucket_retention.rego

Filename convention: `<rule_id>.rego`. The engine resolves a violation's
source file by looking up `<rule_id>.rego` in the dirs that were evaluated.
This keeps the engine zero-config and the report human-readable.

Common-policy gating (P3-4)
---------------------------
Both `common/mandatory_labels.rego` and `common/mandatory_tags.rego` fire
on EVERY resource (GCP + AWS) but each is gated by a precondition
(`input.labels` or `input.Tags`) so only the matching cloud-shape
fires per resource. Without the gates, `not input.labels.team` would
be true for AWS resources (which have no `labels`) and produce a
false-positive flood. With the gates, exactly one of the two rules
fires per resource based on which cloud's snapshot shape is present.
"""

import os

# --- Scope -----------------------------------------------------------------

# Resource types we evaluate. Mirrors detector/config.py IN_SCOPE_TF_TYPES;
# kept independent so policy can be enabled per-type without coupling.
#
# P3-4: aws_instance + aws_s3_bucket added. Phase 4 CG-2 will widen this
# further to match the importer's full type coverage (currently 17 GCP +
# the 2 AWS types here). For Phase 3 we ship only the AWS analogues of
# the existing GCP types so policy authoring is symmetric across clouds
# at the shipped pair.
IN_SCOPE_TF_TYPES = {
    # GCP
    "google_compute_instance",
    "google_storage_bucket",
    # AWS (P3-4)
    "aws_instance",
    "aws_s3_bucket",
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


# --- P4-1 violation caps (defends against malicious / buggy input) ---------

# Maximum violations a single conftest call may surface for one resource.
# Defends against a buggy rule that iterates a long list (e.g. a VM with
# 100 NICs each with 10 access configs => 1000 individual violations) AND
# against a malicious .tf crafted to trigger the same.
#
# Per-call cap is the first defensive layer -- engine.evaluate() truncates
# the returned list at this count and emits a one-line warning. Callers
# see at most this many violations from any single resource.
MAX_VIOLATIONS_PER_CALL = 100

# Maximum total violations across an entire run (all resources combined).
# Defends against the malicious-tf case where 10000 trivial resources each
# produce a few violations, blowing up policy output and downstream
# rendering / log-volume / dashboard-aggregation costs.
#
# Per-run cap is the second defensive layer -- the standalone CLI in
# policy/run.py stops aggregating once the cap is exceeded and emits a
# single warning. The decoration path (policy/integration.py) is per-
# resource and bounded by MAX_VIOLATIONS_PER_CALL alone.
MAX_VIOLATIONS_PER_RUN = 1000
