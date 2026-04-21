# detector/config.py
"""
Drift-detection scope and normalization rules.

For the POC we deliberately limit scope to two resource types so we can
iterate fast on the diff semantics before scaling to ASSET_TO_TERRAFORM_MAP.
"""

# --- Scope: which resource types we will detect drift on (POC) ---
IN_SCOPE_TF_TYPES = {
    "google_compute_instance",
    "google_storage_bucket",
}

# --- Path to the local Terraform state file (POC: local only) ---
# Resolved at runtime relative to the project root.
STATE_FILE_NAME = "terraform.tfstate"

# --- Concurrency for parallel cloud snapshot fetches ---
MAX_SNAPSHOT_WORKERS = 8

# --- Globally-ignored fields ---
# These are *always* dropped from BOTH sides of the diff because they are
# either pure Terraform metadata or pure cloud-side computed/server-set fields
# that no human would ever want to manage.
GLOBAL_IGNORE_FIELDS = {
    # Terraform-state metadata
    "id", "timeouts", "terraform_labels", "effective_labels",
    # GCP universal computed fields
    "self_link", "selfLink",
    "creation_timestamp", "creationTimestamp",
    "fingerprint", "label_fingerprint", "labelFingerprint",
    "etag", "kind", "status", "current_status",
    # GCP API plumbing
    "satisfies_pzs", "satisfiesPzs",
    "satisfies_pzi", "satisfiesPzi",
    "metadata_fingerprint", "metadataFingerprint",
    "tags_fingerprint", "tagsFingerprint",
}

# --- Per-resource ignore: fields that drift constantly and don't matter ---
RESOURCE_IGNORE_FIELDS = {
    "google_compute_instance": {
        # Server-managed GCE attrs
        "cpu_platform", "cpuPlatform",
        "instance_id", "instanceId",
        "last_start_timestamp", "lastStartTimestamp",
        "last_stop_timestamp", "lastStopTimestamp",
        "last_suspended_timestamp",
        "guest_accelerators", "guestAccelerators",  # often drifts as []
        # Already known-noisy from your importer/heuristics.json
        "guest_os_features", "guestOsFeatures",
        "resource_policies", "resourcePolicies",
        "key_revocation_action_type", "keyRevocationActionType",
    },
    "google_storage_bucket": {
        "time_created", "timeCreated",
        "updated",
        "metageneration",
        "project_number", "projectNumber",
        "rpo",  # often present as "DEFAULT" server-side, omitted in HCL
    },
}

# --- URL-prefix stripping ---
# GCP returns full self-links; Terraform state often stores short forms.
# Diff is hopeless without normalizing these.
URL_PREFIXES_TO_STRIP = (
    "https://www.googleapis.com/compute/v1/",
    "https://www.googleapis.com/storage/v1/",
    "https://compute.googleapis.com/compute/v1/",
    "https://storage.googleapis.com/",
)


def is_in_scope(tf_type: str) -> bool:
    return tf_type in IN_SCOPE_TF_TYPES


def fields_to_ignore_for(tf_type: str) -> set:
    """Union of global + per-resource ignore fields, both casings."""
    return GLOBAL_IGNORE_FIELDS | RESOURCE_IGNORE_FIELDS.get(tf_type, set())