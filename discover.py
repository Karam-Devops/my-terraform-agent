# discover.py

import subprocess
import os
import shutil
import json
import csv
from collections import defaultdict

# NEW: Import the native Google Cloud Asset client library
from google.cloud import asset_v1
from google.api_core import exceptions as google_exceptions

# --- Configuration ---
OUTPUT_PATH = "./discovered_infra"
INVENTORY_FILENAME = "gcp_asset_inventory.csv"

def save_assets_to_csv(assets: list, filename: str):
    """Saves a list of discovered assets to a CSV file."""
    print(f"--- Saving asset inventory to '{filename}' ---")
    headers = ["AssetType", "AssetName", "Location", "FullGcpName"]
    with open(filename, 'w', newline='', encoding='utf-8') as csvfile:
        writer = csv.writer(csvfile)
        writer.writerow(headers)
        for asset in assets:
            simple_type = asset.asset_type.split('/')[-1]
            asset_name = asset.name.split('/')[-1]
            location = asset.location
            full_gcp_name = asset.name
            writer.writerow([simple_type, asset_name, location, full_gcp_name])
    print(f"✅ Successfully created inventory file.")

def inventory_gcp_assets(project_id: str) -> dict:
    """
    Uses the native Python client library for Cloud Asset Inventory.
    This is more robust and provides better error handling.
    """
    print(f"\n--- Stage 1: Running Cloud Asset Inventory for {project_id} ---")
    
    # Initialize the client
    client = asset_v1.AssetServiceClient()
    
    asset_types = [
        "compute.googleapis.com/Instance", "compute.googleapis.com/Network",
        "compute.googleapis.com/Subnetwork", "compute.googleapis.com/Firewall",
        "storage.googleapis.com/Bucket", "sqladmin.googleapis.com/Instance"
    ]
    scope = f"projects/{project_id}"
    
    try:
        print("Searching for assets... (this may take a moment)")
        
        # Construct the request and call the API
        request = asset_v1.SearchAllResourcesRequest(
            scope=scope,
            asset_types=asset_types,
        )
        response_iterator = client.search_all_resources(request=request)
        
        # The response is an iterator of Asset objects
        assets = list(response_iterator)

        if not assets:
            print("No supported assets found in the specified project.")
            return None

        save_assets_to_csv(assets, INVENTORY_FILENAME)

        categorized_assets = defaultdict(list)
        for asset in assets:
            simple_type = asset.asset_type.split('/')[-1]
            categorized_assets[simple_type].append(asset)
            
        print("\n--- Discovered Asset Summary ---")
        for asset_type, asset_list in categorized_assets.items():
            print(f"  - {asset_type}: {len(asset_list)}")
        
        return categorized_assets

    except google_exceptions.PermissionDenied as e:
        print("❌ ERROR: Permission denied. The account used for authentication does not have the 'cloudasset.assets.searchAllResources' permission.")
        print(f"   Details: {e.message}")
        return None
    except google_exceptions.FailedPrecondition as e:
        # This is the specific error for a disabled API
        if "service is not enabled" in e.message:
            print("❌ ERROR: The Cloud Asset API (cloudasset.googleapis.com) is not enabled for this project.")
            print("   Please enable it in the Google Cloud Console and try again.")
        else:
            print(f"❌ ERROR: A precondition failed. Details: {e.message}")
        return None
    except Exception as e:
        print(f"An unexpected error occurred: {e}")
        return None

def get_user_selection(categorized_assets: dict) -> list:
    """
    Displays a menu of discovered resources and prompts the user for a selection.
    """
    print("\n--- Step 2: Select Resources to Export ---")
    selection_map = {i + 1: asset_type for i, asset_type in enumerate(categorized_assets.keys())}
    
    for i, asset_type in selection_map.items():
        print(f"  [{i}] {asset_type} ({len(categorized_assets[asset_type])} found)")
    print(f"  [all] All of the above")

    while True:
        choice = input("\nEnter the numbers of the resources to export (e.g., 1,3), or 'all': ").lower()
        if choice == 'all':
            return list(selection_map.values())
        try:
            selected_indices = [int(i.strip()) for i in choice.split(',')]
            selected_types = [selection_map[i] for i in selected_indices if i in selection_map]
            if selected_types and len(selected_types) == len(selected_indices):
                print(f"You have selected: {', '.join(selected_types)}")
                return selected_types
            else:
                print("Invalid selection. Please enter numbers from the list.")
        except ValueError:
            print("Invalid input. Please enter numbers separated by commas, or 'all'.")

def export_gcp_hcl(project_id: str, resource_types_to_export: list):
    """Exports the selected resources to Terraform HCL using the gcloud command."""
    print(f"\n--- Stage 3: Exporting selected resources to Terraform HCL ---")
    
    gcloud_resource_types_map = {
        "Instance": "ComputeInstance", "Bucket": "StorageBucket",
        "Network": "ComputeNetwork", "Subnetwork": "ComputeSubnetwork",
        "Firewall": "ComputeFirewall", "Instance": "SQLInstance"
    }
    gcloud_types_string = ",".join([gcloud_resource_types_map.get(t, t) for t in resource_types_to_export])

    # Note: We still use subprocess for this part, as there is no native Python library
    # for 'gcloud beta resource-config bulk-export'. This is a perfect hybrid approach.
    command = ["gcloud", "beta", "resource-config", "bulk-export", f"--project={project_id}",
               f"--resource-types={gcloud_types_string}", "--resource-format=terraform", f"--path={OUTPUT_PATH}"]
    
    try:
        print(f"Running command: {' '.join(command)}")
        subprocess.run(command, check=True, capture_output=True, text=True)
        print(f"✅ Success! Selected infrastructure exported to '{OUTPUT_PATH}' directory.")
    except subprocess.CalledProcessError as e:
        print(f"❌ ERROR: The gcloud bulk-export command failed.\n--- STDERR ---\n{e.stderr}")

def discover_gcp():
    """Orchestrates the full discovery and export workflow for GCP."""
    project_id = input("Please enter the GCP Project ID to scan: ")
    if not project_id: return

    categorized_assets = inventory_gcp_assets(project_id)
    if not categorized_assets:
        print("\nHalting process due to inventory failure.")
        return

    selected_types = get_user_selection(categorized_assets)
    if not selected_types:
        print("No resources selected. Exiting.")
        return
        
    proceed = input(f"\nProceed with exporting the {len(selected_types)} selected resource type(s) to HCL? (y/n): ").lower()
    if proceed == 'y':
        export_gcp_hcl(project_id, selected_types)
    else:
        print("Export cancelled by user.")

# --- discover_aws() and main() functions are unchanged ---
def discover_aws():
    """Placeholder function for discovering resources from AWS."""
    print("\nNOTE: AWS discovery is not yet implemented.")

def main():
    """Main function to orchestrate infrastructure discovery."""
    if os.path.exists(OUTPUT_PATH):
        shutil.rmtree(OUTPUT_PATH)
    if os.path.exists(INVENTORY_FILENAME):
        os.remove(INVENTORY_FILENAME)

    target_cloud = input("Which cloud do you want to discover resources from? (gcp/aws): ").lower()

    if target_cloud == "gcp":
        discover_gcp()
    elif target_cloud == "aws":
        discover_aws()
    else:
        print("Invalid selection. Please choose 'gcp' or 'aws'.")

if __name__ == "__main__":
    main()