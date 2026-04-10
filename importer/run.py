# importer/run.py
import os
# --- THIS IS THE DEFINITIVE FIX ---
# This code block ensures that no matter where the script is run from,
# its "Current Working Directory" is always the main project folder.
# This solves all file creation and relative path issues.
project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
os.chdir(project_root)
print(f"--- Ensuring working directory is set to: {os.getcwd()} ---")
# --- END OF FIX ---

# Now that the context is correct, we can use clean relative imports.
from . import config, gcp_client, terraform_client, hcl_generator

def _present_selection_menu(resources):
    print("\n--- Stage 2: Select a Resource to Import ---")
    for i, resource in enumerate(resources):
        display_name = resource.get('displayName', resource.get('name'))
        asset_type_short = resource.get('assetType').split('/')[-1]
        location = resource.get('location', 'N/A')
        print(f"  [{i + 1}] {display_name:<35} (Type: {asset_type_short:<10} | Location: {location})")
    while True:
        try:
            choice = int(input("\nEnter the number of the resource (or 0 to cancel): "))
            if 0 == choice: return None
            if 1 <= choice <= len(resources): return resources[choice - 1]
            else: print("❌ Invalid number.")
        except ValueError: print("❌ Please enter a valid number.")

def _map_asset_to_terraform(selected_asset, project_id):
    print("\n--- Stage 3: Mapping Asset to Terraform ---")
    resource_name = selected_asset.get('displayName') or selected_asset['name'].split('/')[-1]
    asset_type = selected_asset['assetType']
    tf_type = config.ASSET_TO_TERRAFORM_MAP[asset_type]
    mapping = {
        "tf_type": tf_type, "hcl_name": resource_name.replace('-', '_'),
        "resource_name": resource_name, "import_id": selected_asset['name'].split('/', 2)[-1],
        "filename": f"{tf_type}_{resource_name.replace('-', '_')}.tf",
        "location": selected_asset.get('location'), "project_id": project_id,
    }
    print("✅ Mapping successful."); return mapping

def run_workflow():
    print("🚀 Starting Google Cloud to Terraform Import Workflow...")
    if not os.path.isdir(".terraform"):
        if terraform_client.init() is None: return

    project_id = input("Enter your Google Cloud Project ID: ")
    print("\n--- Stage 1: Discovering All Supported Resources ---")
    all_resources = [res for asset_type in config.ASSET_TO_TERRAFORM_MAP for res in gcp_client.discover_resources_of_type(project_id, asset_type)]
    if not all_resources:
        print("\n🏁 No supported resources found."); return

    selected_asset = _present_selection_menu(all_resources)
    if not selected_asset:
        print("\nOperation cancelled."); return

    mapping = _map_asset_to_terraform(selected_asset, project_id)
    resource_json = gcp_client.get_resource_details_json(mapping)
    if not resource_json:
        print("❌ Workflow stopped because resource details could not be retrieved."); return
    
    generated_hcl = hcl_generator.generate_hcl_from_json(resource_json, mapping['tf_type'], mapping['hcl_name'])
    if not generated_hcl: return

    with open(mapping["filename"], "w", encoding='utf-8') as f: f.write(generated_hcl)
    print(f"\n✅ Saved LLM-generated HCL to: {mapping['filename']}")

    if terraform_client.import_resource(mapping): terraform_client.plan()
    print("\nWorkflow finished.")

if __name__ == "__main__":
    run_workflow()