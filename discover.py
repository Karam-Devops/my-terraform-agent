# discover.py

import subprocess
import os
import shutil
import json
import csv
from collections import defaultdict

# --- Configuration ---
OUTPUT_PATH = "./discovered_infra"
INVENTORY_FILENAME = "gcp_asset_inventory.csv"

# The paths for the gcloud executable and its root directory.
GCLOUD_CMD_PATH = "C:\\Program Files (x86)\\Google\\Cloud SDK\\google-cloud-sdk\\bin\\gcloud.cmd"
GCLOUD_SDK_ROOT_PATH = "C:\\Program Files (x86)\\Google\\Cloud SDK"


def save_assets_to_csv(assets: list, filename: str):
    """Saves a list of discovered assets to a CSV file."""
    print(f"--- Saving asset inventory to '{filename}' ---")
    headers = ["AssetType", "AssetName", "Location", "FullGcpName"]
    with open(filename, 'w', newline='', encoding='utf-8') as csvfile:
        writer = csv.writer(csvfile)
        writer.writerow(headers)
        for asset in assets:
            simple_type = asset.get('assetType', 'U').split('/')[-1]
            asset_name = asset.get('name', 'U').split('/')[-1]
            location = asset.get('location', 'global')
            full_gcp_name = asset.get('name', 'U')
            writer.writerow([simple_type, asset_name, location, full_gcp_name])
    print(f"✅ Successfully created inventory file.")

def inventory_gcp_assets(project_id: str) -> dict:
    """Uses Cloud Asset Inventory and returns a categorized dictionary of assets."""
    print(f"\n--- Stage 1: Running Cloud Asset Inventory for {project_id} ---")
    asset_types = "compute.googleapis.com/Instance,storage.googleapis.com/Bucket,sqladmin.googleapis.com/Instance"
    
    # --- MODIFIED: Added the --quiet flag to ensure no interactive prompts ---
    command = [
        GCLOUD_CMD_PATH, "--quiet", "asset", "search-all-resources",
        f"--scope=projects/{project_id}", f"--asset-types={asset_types}", "--format=json"
    ]
    
    try:
        print("Searching for assets...")
        result = subprocess.run(command, check=True, capture_output=True, text=True, shell=True)
        assets = json.loads(result.stdout)
        
        if not assets:
            print("No supported assets found."); return None

        save_assets_to_csv(assets, INVENTORY_FILENAME)
        categorized_assets = defaultdict(list)
        for asset in assets:
            categorized_assets[asset.get('assetType', 'U').split('/')[-1]].append(asset)
            
        print("\n--- Discovered Asset Summary ---")
        for asset_type, asset_list in categorized_assets.items():
            print(f"  - {asset_type}: {len(asset_list)}")
        
        return categorized_assets
    except FileNotFoundError:
        print(f"❌ ERROR: Executable not found at '{GCLOUD_CMD_PATH}'"); return None
    except subprocess.CalledProcessError as e:
        if "service is not enabled" in e.stderr:
            print("❌ ERROR: The Cloud Asset API is not enabled.")
        else:
            print(f"❌ ERROR: gcloud asset command failed.\n{e.stderr}")
        return None

def get_user_selection(categorized_assets: dict) -> list:
    """Displays a menu and prompts the user for a selection."""
    print("\n--- Step 2: Select Resources to Export ---")
    selection_map = {i + 1: asset_type for i, asset_type in enumerate(categorized_assets.keys())}
    for i, asset_type in selection_map.items():
        print(f"  [{i}] {asset_type} ({len(categorized_assets[asset_type])} found)")
    print(f"  [all] All of the above")
    while True:
        choice = input("\nEnter numbers to export (e.g., 1,3), or 'all': ").lower()
        if choice == 'all': return list(selection_map.values())
        try:
            selected_indices = [int(i.strip()) for i in choice.split(',')]
            selected_types = [selection_map[i] for i in selected_indices if i in selection_map]
            if selected_types and len(selected_indices) == len(selected_indices):
                print(f"You selected: {', '.join(selected_types)}"); return selected_types
            else: print("Invalid selection.")
        except ValueError: print("Invalid input.")

def export_gcp_hcl(project_id: str, resource_types_to_export: list):
    """Exports the selected resources to Terraform HCL."""
    print(f"\n--- Stage 3: Exporting selected resources to Terraform HCL ---")
    gcloud_resource_types_map = {"Instance": "ComputeInstance", "Bucket": "StorageBucket", "SQLInstance": "SQLInstance"}
    gcloud_types_string = ",".join([gcloud_resource_types_map.get(t, t) for t in resource_types_to_export])
    absolute_output_path = os.path.abspath(OUTPUT_PATH)
    print(f"Output will be written to absolute path: {absolute_output_path}")

    # --- MODIFIED: Added the --quiet flag to prevent the command from hanging ---
    command = [
        GCLOUD_CMD_PATH, "--quiet", "beta", "resource-config", "bulk-export",
        f"--project={project_id}", f"--resource-types={gcloud_types_string}",
        "--resource-format=terraform", f"--path={absolute_output_path}"
    ]
    
    try:
        print(f"Running command: {' '.join(command)}")
        # The timeout is a safety net. If it still hangs for more than 5 minutes, it will crash with a clear error.
        subprocess.run(
            command, check=True, capture_output=True, text=True, shell=True,
            cwd=GCLOUD_SDK_ROOT_PATH,
            timeout=300 # Add a 5-minute timeout as a safety measure
        )
        print(f"✅ Success! Selected infrastructure exported to '{absolute_output_path}' directory.")
    except subprocess.TimeoutExpired:
        print("❌ ERROR: The gcloud command timed out after 5 minutes. The process is likely stuck.")
    except subprocess.CalledProcessError as e:
        print(f"❌ ERROR: The gcloud bulk-export command failed.\n--- STDERR ---\n{e.stderr}")

def discover_gcp():
    # Orchestration logic remains the same
    project_id = input("Please enter the GCP Project ID to scan: ")
    if not project_id: return
    categorized_assets = inventory_gcp_assets(project_id)
    if not categorized_assets: print("\nHalting process."); return
    selected_types = get_user_selection(categorized_assets)
    if not selected_types: print("No resources selected. Exiting."); return
    proceed = input(f"\nProceed with exporting {len(selected_types)} selected resource type(s)? (y/n): ").lower()
    if proceed == 'y':
        export_gcp_hcl(project_id, selected_types)
    else:
        print("Export cancelled.")

def discover_aws():
    """Placeholder function for discovering resources from AWS."""
    print("\nNOTE: AWS discovery is not yet implemented.")

def main():
    # Main logic remains the same
    if os.path.exists(OUTPUT_PATH): shutil.rmtree(OUTPUT_PATH)
    if os.path.exists(INVENTORY_FILENAME): os.remove(INVENTORY_FILENAME)
    target_cloud = input("Which cloud do you want to discover from? (gcp/aws): ").lower()
    if target_cloud == "gcp":
        discover_gcp()
    elif target_cloud == "aws":
        discover_aws()
    else:
        print("Invalid selection.")

if __name__ == "__main__":
    main()