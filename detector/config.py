# detector/config.py
"""
Drift-detection scope and normalization rules.

For the POC we deliberately limit scope to two resource types so we can
iterate fast on the diff semantics before scaling to ASSET_TO_TERRAFORM_MAP.
"""

import json
import os

# --- Scope: which resource types we will detect drift on (POC) ---
IN_SCOPE_TF_TYPES = {
    "google_compute_instance",
    "google_storage_bucket",
}

# --- Path to the local Terraform state file (POC: local only) ---
STATE_FILE_NAME = "terraform.tfstate"

# --- Concurrency for parallel cloud snapshot fetches ---
MAX_SNAPSHOT_WORKERS = 8

# --- Globally-ignored fields ---
# Always dropped from BOTH sides of the diff. Pure metadata, computed, or
# server-set fields that no human would ever want to manage.
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

# --- Per-resource: fields that drift constantly and don't matter ---
RESOURCE_IGNORE_FIELDS = {
    "google_compute_instance": {
        # Server-managed runtime attrs
        "cpu_platform", "cpuPlatform",
        "instance_id", "instanceId",
        "last_start_timestamp", "lastStartTimestamp",
        "last_stop_timestamp", "lastStopTimestamp",
        "last_suspended_timestamp",
        "guest_accelerators", "guestAccelerators",
        # Cloud-only diagnostics with no HCL equivalent
        "start_restricted", "startRestricted",
        "resource_status", "resourceStatus",
        "shielded_instance_integrity_policy", "shieldedInstanceIntegrityPolicy",
        # State-only metadata (cloud omits 'project' — implicit in URL path)
        "project",
        "subnetwork_project",  # state-only bookkeeping inside network_interface
        # Already known-noisy from importer/heuristics.json
        "guest_os_features", "guestOsFeatures",
        "resource_policies", "resourcePolicies",
        "key_revocation_action_type", "keyRevocationActionType",
    },
    "google_storage_bucket": {
        "time_created", "timeCreated",
        "updated",
        "metageneration",
        "project_number", "projectNumber",
        "rpo",
        "project",
    },
}

# --- Per-resource: complex blocks the deterministic diff cannot align ---
# These need a bespoke normalizer (planned for v2). For now we suppress
# them on BOTH sides and document the limitation.
COMPLEX_BLOCKS_TO_SKIP = {
    "google_compute_instance": {
        # Cloud `disks` is a flat list of all disks (boot + attached).
        # State splits them into `boot_disk` and `attached_disk`. Aligning
        # them needs a normalizer that knows about the `boot=True` flag.
        "disks",
        "boot_disk",
        "attached_disk",
        # Cloud encodes display state as `display_device.enable_display`,
        # state encodes it as scalar `enable_display`. Trivial to lift but
        # left for v2 normalizer for symmetry with disks.
        "display_device", "displayDevice",
        "enable_display", "enableDisplay",
    },
    "google_storage_bucket": set(),
}

# --- Per-resource: cloud field name -> state field name ---
# Applied during cloud normalization, after camelCase -> snake_case. The
# Google TF provider renames many GCP API plurals to singular HCL forms.
FIELD_ALIASES = {
    "google_compute_instance": {
        # Top-level
        "network_interfaces": "network_interface",
        "service_accounts": "service_account",
        # Inside network_interface
        "access_configs": "access_config",
        # Inside reservation_affinity (TF flattens this rename across nesting;
        # POC accepts the small risk of cross-nesting collision since
        # `consume_reservation_type` is a unique GCP API field name).
        "consume_reservation_type": "type",
    },
    "google_storage_bucket": {},
}

# --- Per-resource: path-scoped ignores (canonical paths, no list indices) ---
# Used when a field name is fine at the top level but should be ignored
# inside a particular nested context. Path uses dot notation; list indices
# are stripped before matching ('a[0].b' matches the rule 'a.b').
PATH_IGNORE_FIELDS = {
    "google_compute_instance": {
        # Server-set when an external NAT access config is present.
        "network_interface.access_config.name",
        "network_interface.access_config.type",
    },
    "google_storage_bucket": set(),
}

# --- Per-resource: fields whose value is a `projects/.../<leaf>` URL on the
# cloud side but a bare leaf on the state side. We strip cloud to its leaf.
LEAF_ONLY_FIELDS = {
    "google_compute_instance": {
        "machine_type",
        "zone",
    },
    "google_storage_bucket": set(),
}

# --- URL-prefix stripping (full https URLs) ---
URL_PREFIXES_TO_STRIP = (
    "https://www.googleapis.com/compute/v1/",
    "https://www.googleapis.com/storage/v1/",
    "https://compute.googleapis.com/compute/v1/",
    "https://storage.googleapis.com/",
)


# --- Heuristics integration ----------------------------------------------
# Anything marked OMIT or IGNORE in importer/heuristics.json should also be
# silently ignored for drift purposes — those are fields we already decided
# we cannot or will not manage.
def _load_heuristics_ignores() -> dict:
    """Returns {tf_type: {field, ...}} derived from importer rules."""
    heuristics_path = os.path.join(
        os.path.dirname(__file__), os.pardir, "importer", "heuristics.json"
    )
    if not os.path.isfile(heuristics_path):
        return {}
    try:
        with open(heuristics_path, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except (json.JSONDecodeError, IOError):
        return {}

    out: dict = {}
    for tf_type, rules in raw.items():
        if not isinstance(rules, dict):
            continue
        ignored = set()
        for field, rule in rules.items():
            if not isinstance(rule, str):
                continue
            cmd = rule.strip().upper()
            if cmd == "OMIT" or cmd.startswith("IGNORE"):
                ignored.add(field)
        if ignored:
            out[tf_type] = ignored
    return out


_HEURISTICS_IGNORES = _load_heuristics_ignores()


# --- Public accessors ----------------------------------------------------

def is_in_scope(tf_type: str) -> bool:
    return tf_type in IN_SCOPE_TF_TYPES


def fields_to_ignore_for(tf_type: str) -> set:
    """
    Union of:
      - global ignores (apply to every resource)
      - per-resource ignores (curated)
      - complex blocks the POC cannot diff yet
      - heuristics-derived ignores (live merge from importer/heuristics.json)
    """
    return (
        GLOBAL_IGNORE_FIELDS
        | RESOURCE_IGNORE_FIELDS.get(tf_type, set())
        | COMPLEX_BLOCKS_TO_SKIP.get(tf_type, set())
        | _HEURISTICS_IGNORES.get(tf_type, set())
    )


def aliases_for(tf_type: str) -> dict:
    return FIELD_ALIASES.get(tf_type, {})


def leaf_only_fields_for(tf_type: str) -> set:
    return LEAF_ONLY_FIELDS.get(tf_type, set())


def path_ignore_for(tf_type: str) -> set:
    return PATH_IGNORE_FIELDS.get(tf_type, set())
