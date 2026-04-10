# importer/run.py
import os
from . import config, gcp_client, terraform_client, hcl_generator

# This block ensures the working directory is always the project root.
project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
os.chdir(project_root)
print(f"--- Ensuring working directory is set to: {os.getcwd()} ---")

def _present_selection_menu(resources):
    """Presents an interactive selection menu to the user."""
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
    """Creates a mapping dictionary from the selected asset's details."""
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
    print("✅ Mapping successful.")
    return mapping

# ---------------------------------------------------------------------------------
# THE FIX IS IN THIS FUNCTION
# ---------------------------------------------------------------------------------
def run_workflow():
    """The main orchestration function, now with a self-correction loop."""
    print("🚀 Starting Google Cloud to Terraform Import Workflow...")
    if not os.path.isdir(".terraform"):
        if terraform_client.init() is None: return

    project_id = input("Enter your Google Cloud Project ID: ")
    
    print("\n--- Stage 1: Discovering All Supported Resources ---")
    all_resources = [res for asset_type in config.ASSET_TO_TERRAFORM_MAP for res in gcp_client.discover_resources_of_type(project_id, asset_type)]
    
    if not all_resources:
        print("\n🏁 No supported resources found. Workflow finished.")
        return

    selected_asset = _present_selection_menu(all_resources)
    if not selected_asset:
        print("\nOperation cancelled by user.")
        return

    # --- THIS IS THE MISSING LINE THAT IS NOW RE-ADDED ---
    mapping = _map_asset_to_terraform(selected_asset, project_id)
    if not mapping:
        print("❌ Failed to create mapping from selected asset.")
        return
    # --- END OF FIX ---

    # --- Initial Generation & Import (One-Time) ---
    print("\n--- Generating Initial HCL (Attempt 1) ---")
    resource_json = gcp_client.get_resource_details_json(mapping)
    if not resource_json: return

    generated_hcl = hcl_generator.generate_hcl_from_json(
        resource_json, mapping['tf_type'], mapping['hcl_name'], attempt=1
    )
    if not generated_hcl: return

    with open(mapping["filename"], "w", encoding='utf-8') as f: f.write(generated_hcl)
    print(f"\n✅ Saved initial HCL to: {mapping['filename']}")

    if not terraform_client.import_resource(mapping): return

    # --- Self-Correction & Verification Loop ---
    for attempt in range(config.MAX_LLM_RETRIES):
        print(f"\n--- Verification Cycle: Attempt {attempt + 1} of {config.MAX_LLM_RETRIES} ---")

        is_success, plan_output_or_error = terraform_client.plan()

        if is_success:
            return # Workflow is complete and successful

        # If we are here, the plan failed.
        print("   - Plan failed. Attempting self-correction...")

        if attempt < config.MAX_LLM_RETRIES - 1:
            corrected_hcl = hcl_generator.generate_hcl_from_json(
                resource_json, mapping['tf_type'], mapping['hcl_name'], 
                attempt=attempt + 2,
                previous_error=plan_output_or_error
            )
            if corrected_hcl:
                with open(mapping["filename"], "w", encoding='utf-8') as f: f.write(corrected_hcl)
                print(f"\n✅ Saved corrected HCL to: {mapping['filename']}")
            else:
                print("   - LLM failed to provide a correction. Aborting loop.")
                break
        else:
            print(f"\n❌ Maximum retries ({config.MAX_LLM_RETRIES}) reached. Auto-correction failed.")
            print("   Please manually inspect the last error and the generated HCL file.")
            break

    print("\nWorkflow finished.")

if __name__ == "__main__":
    run_workflow()