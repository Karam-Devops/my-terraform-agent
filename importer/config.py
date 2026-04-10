# importer/config.py

# --- CLI Paths ---
# Use raw strings (r"...") for Windows paths.
TERRAFORM_PATH = r"C:\Terraform\terraform.exe"
GCLOUD_CMD_PATH = r"C:\Program Files (x86)\Google\Cloud SDK\google-cloud-sdk\bin\gcloud.cmd"

# --- Mappings ---

# Maps Google Cloud Asset Types to their corresponding Terraform resource types.
ASSET_TO_TERRAFORM_MAP = {
    "compute.googleapis.com/Firewall": "google_compute_firewall",
    "compute.googleapis.com/Instance": "google_compute_instance",
    "compute.googleapis.com/Disk": "google_compute_disk",
    "sqladmin.googleapis.com/Instance": "google_sql_database_instance",
    # This entry correctly maps the asset type to the terraform type
    "storage.googleapis.com/Bucket": "google_storage_bucket",
}

# Maps Terraform resource types to the gcloud commands needed to get their full details.
TF_TYPE_TO_GCLOUD_INFO = {
    "google_compute_firewall": {
        "describe_command": "compute firewall-rules describe"
    },
    "google_compute_instance": {
        "describe_command": "compute instances describe",
        "zone_flag": "--zone"
    },
    "google_compute_disk": {
        "describe_command": "compute disks describe",
        "zone_flag": "--zone"
    },
    "google_sql_database_instance": {
        "describe_command": "sql instances describe"
    },
    # THIS IS THE ENTRY THAT WAS MISSING
    # It tells the script how to get details for 'google_storage_bucket'
    "google_storage_bucket": {
        "describe_command": "storage buckets describe",
        "name_format": "gs://{name}"  # Special formatting for this command
    }
}