# importer/config.py

from common.terraform_path import resolve_terraform_path as _resolve_terraform

# --- CLI Paths ---
# TERRAFORM_PATH is resolved lazily on first attribute access (see __getattr__
# at the bottom of this module). Three places used to hardcode the Windows
# install path here, in translator/config.py, and in agent_nodes.py — that
# broke on every machine where Terraform lived elsewhere or was only on PATH.
# The resolver checks $TERRAFORM_BINARY → platform default → PATH → fail.
GCLOUD_CMD_PATH = r"C:\Program Files (x86)\Google\Cloud SDK\google-cloud-sdk\bin\gcloud.cmd"

# --- Concurrency Settings ---
MAX_DISCOVERY_WORKERS = 16
MAX_IMPORT_WORKERS = 8

# --- Self-Correction Loop ---
MAX_LLM_RETRIES = 5

# --- Mappings ---

ASSET_TO_TERRAFORM_MAP = {
    # GCE
    "compute.googleapis.com/Instance": "google_compute_instance",
    "compute.googleapis.com/Disk": "google_compute_disk",
    "compute.googleapis.com/Firewall": "google_compute_firewall",
    "compute.googleapis.com/Address": "google_compute_address",
    "compute.googleapis.com/Network": "google_compute_network",
    "compute.googleapis.com/Subnetwork": "google_compute_subnetwork",
    "compute.googleapis.com/InstanceTemplate": "google_compute_instance_template",
    
    # GKE
    "container.googleapis.com/Cluster": "google_container_cluster",
    "container.googleapis.com/NodePool": "google_container_node_pool",
    
    # IAM
    "iam.googleapis.com/ServiceAccount": "google_service_account",
    
    # Cloud Storage
    "storage.googleapis.com/Bucket": "google_storage_bucket",

    # Cloud SQL
    "sqladmin.googleapis.com/Instance": "google_sql_database_instance",

    # KMS (P2-3) -- enables CMEK demos: bucket / disk encryption violations
    # from the Policy engine become "create one of these and reference it"
    # actionable steps. Crypto keys are nested inside key rings (parent
    # path segment "keyRings"); see gcp_client.extract_path_segment +
    # _map_asset_to_terraform for the parent-name extraction.
    "cloudkms.googleapis.com/KeyRing": "google_kms_key_ring",
    "cloudkms.googleapis.com/CryptoKey": "google_kms_crypto_key",

    # Cloud Run v2 (P2-4) -- the modern (post-2023) Cloud Run resource
    # shape. The legacy `google_cloud_run_service` (v1) is deprecated;
    # all new deployments use v2. Maps from the same asset type as v1
    # (run.googleapis.com/Service) -- the v1/v2 distinction is in the
    # provider's HCL surface, not in Cloud Asset Inventory's URN scheme.
    "run.googleapis.com/Service": "google_cloud_run_v2_service",

    # Pub/Sub (P2-5) -- topic + subscription. Both are PROJECT-SCOPED
    # (global, no per-location config), so neither declares a
    # zone/region/location flag in TF_TYPE_TO_GCLOUD_INFO. Subscription
    # references topic via the `topic` attribute in HCL (a full
    # `projects/<P>/topics/<T>` URN string), so no parent-flag is
    # needed at describe time -- the relationship is rebuilt by
    # terraform import from the URN literal.
    "pubsub.googleapis.com/Topic": "google_pubsub_topic",
    "pubsub.googleapis.com/Subscription": "google_pubsub_subscription",
}

# This dictionary now contains the definitive information for describe commands AND import ID formats.
TF_TYPE_TO_GCLOUD_INFO = {
    # GCE
    "google_compute_instance": {
        "describe_command": "compute instances describe", 
        "zone_flag": "--zone",
        "import_id_format": "projects/{project}/zones/{zone}/instances/{name}" # NEW
    },
    "google_compute_disk": {
        "describe_command": "compute disks describe", 
        "zone_flag": "--zone",
        "import_id_format": "projects/{project}/zones/{zone}/disks/{name}" # NEW
    },
    "google_compute_firewall": {
        "describe_command": "compute firewall-rules describe",
        "import_id_format": "{project}/{name}" # NEW
    },
    "google_compute_address": {
        "describe_command": "compute addresses describe", 
        "region_flag": "--region",
        "import_id_format": "{project}/{region}/{name}" # NEW
    },
    "google_compute_network": {
        "describe_command": "compute networks describe",
        "import_id_format": "{project}/{name}" # NEW
    },
    "google_compute_subnetwork": {
        "describe_command": "compute networks subnets describe", 
        "region_flag": "--region",
        "import_id_format": "{project}/{region}/{name}" # NEW
    },
    "google_compute_instance_template": {
        "describe_command": "compute instance-templates describe",
        "import_id_format": "{project}/{name}" # NEW
    },

    # GKE -- dual-mode (zonal OR regional). Both flags declared so the
    # describe-side picker in gcp_client._resolve_location_flag can choose
    # `--zone` or `--region` based on the shape of mapping["location"].
    # Without `region_flag`, regional clusters and their node pools used
    # to crash with "Underspecified resource -- please specify --region"
    # (C5.1 fix; surfaced by the Phase 1 SMOKE against a regional
    # Autopilot cluster). The `import_id_format` placeholder name is
    # `{zone}` for back-compat — at format time both `zone` and `region`
    # format vars carry the same location value, so the URL works either
    # way.
    "google_container_cluster": {
        "describe_command": "container clusters describe",
        "zone_flag": "--zone", "region_flag": "--region",
        "import_id_format": "projects/{project}/locations/{zone}/clusters/{name}" # NEW
    },
    "google_container_node_pool": {
        "describe_command": "container node-pools describe",
        "cluster_flag": "--cluster", "zone_flag": "--zone", "region_flag": "--region",
        "import_id_format": "projects/{project}/locations/{zone}/clusters/{cluster}/nodePools/{name}" # NEW
    },
    
    # IAM
    "google_service_account": {
        "describe_command": "iam service-accounts describe",
        "import_id_format": "{project}/{email}" # NEW - Uses email, not name
    },

    # Cloud Storage
    "google_storage_bucket": {
        "describe_command": "storage buckets describe", 
        "name_format": "gs://{name}",
        "import_id_format": "{project}/{name}" # NEW
    },

    # Cloud SQL
    "google_sql_database_instance": {
        "describe_command": "sql instances describe",
        "import_id_format": "{project}/{name}" # NEW
    },

    # KMS (P2-3) -- KMS uses `--location` (not --zone or --region) because
    # KMS locations span zonal-style regions ("us-central1"), multi-regions
    # ("us"), and a special "global" tier. None fit the zone/region picker
    # in gcp_client._resolve_location_flag, so we declare the third option:
    # `location_flag` -- always emit `--location <value>` regardless of
    # location-string shape.
    "google_kms_key_ring": {
        "describe_command": "kms keyrings describe",
        "location_flag": "--location",
        "import_id_format": "projects/{project}/locations/{location}/keyRings/{name}",
    },
    # google_kms_crypto_key is NESTED under a key ring (parent segment
    # "keyRings"). The keyring name is extracted from the asset path by
    # run.py _map_asset_to_terraform via gcp_client.extract_path_segment,
    # threaded onto the mapping as `mapping["keyring"]`, and wired into
    # `--keyring <name>` by gcp_client.get_resource_details_json. Mirrors
    # the C5 cluster_flag pattern for google_container_node_pool.
    "google_kms_crypto_key": {
        "describe_command": "kms keys describe",
        "location_flag": "--location",
        "keyring_flag": "--keyring",
        "import_id_format": "projects/{project}/locations/{location}/keyRings/{keyring}/cryptoKeys/{name}",
    },

    # Cloud Run v2 (P2-4) -- regional (one location-string per service,
    # always a region like us-central1; no zonal or multi-region option).
    # `--region` is the gcloud flag. import_id format is the full
    # projects/<P>/locations/<R>/services/<N> URN (Cloud Run accepts
    # both this and the shorter `<P>/<R>/<N>` form; we use the full URN
    # for consistency with KMS and GKE clusters).
    "google_cloud_run_v2_service": {
        "describe_command": "run services describe",
        "region_flag": "--region",
        "import_id_format": "projects/{project}/locations/{region}/services/{name}",
    },

    # Pub/Sub (P2-5) -- both topic and subscription are PROJECT-SCOPED
    # (no location). gcloud doesn't take a location flag for either.
    # The import_id format is the short `<project>/<name>` form that
    # `terraform import google_pubsub_topic.<label> <id>` accepts.
    "google_pubsub_topic": {
        "describe_command": "pubsub topics describe",
        "import_id_format": "projects/{project}/topics/{name}",
    },
    "google_pubsub_subscription": {
        "describe_command": "pubsub subscriptions describe",
        "import_id_format": "projects/{project}/subscriptions/{name}",
    },
}

# --- NEW AND FINAL: GitHub Documentation Path Mapping ---
# Maps a Terraform resource type to its specific path component in the GitHub URL.
# This handles exceptions like 'google_service_account'.
TF_TYPE_TO_GITHUB_DOC_PATH = {
    "google_compute_instance": "compute_instance",
    "google_compute_disk": "compute_disk",
    "google_compute_firewall": "compute_firewall",
    "google_compute_address": "compute_address",
    "google_compute_network": "compute_network",
    "google_compute_subnetwork": "compute_subnetwork",
    "google_compute_instance_template": "compute_instance_template", # Another slight exception
    "google_container_cluster": "container_cluster",
    "google_container_node_pool": "container_node_pool",
    "google_kms_key_ring": "kms_key_ring",       # P2-3
    "google_kms_crypto_key": "kms_crypto_key",   # P2-3
    "google_cloud_run_v2_service": "cloud_run_v2_service",  # P2-4
    "google_pubsub_topic": "pubsub_topic",       # P2-5
    "google_pubsub_subscription": "pubsub_subscription",  # P2-5
    "google_service_account": "google_service_account", # The special case you found
    "google_storage_bucket": "storage_bucket",
    "google_sql_database_instance": "sql_database_instance",
}


# --- Lazy attributes (PEP 562) ---
# Existing callers do `config.TERRAFORM_PATH`. Resolving at import time
# would (a) force every module that imports this file to fail at import
# if Terraform isn't installed yet, and (b) freeze the value before the
# Cloud Run image's TERRAFORM_BINARY env var has a chance to apply. So
# we defer resolution to first access.
def __getattr__(name):
    if name == "TERRAFORM_PATH":
        return _resolve_terraform()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")