# importer/config.py

# --- CLI Paths ---
TERRAFORM_PATH = r"C:\Terraform\terraform.exe"
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

    # GKE
    "google_container_cluster": {
        "describe_command": "container clusters describe", 
        "zone_flag": "--zone",
        "import_id_format": "projects/{project}/locations/{zone}/clusters/{name}" # NEW
    },
    "google_container_node_pool": {
        "describe_command": "container node-pools describe", 
        "cluster_flag": "--cluster", "zone_flag": "--zone",
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
    }
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
    "google_service_account": "google_service_account", # The special case you found
    "google_storage_bucket": "storage_bucket",
    "google_sql_database_instance": "sql_database_instance",
}