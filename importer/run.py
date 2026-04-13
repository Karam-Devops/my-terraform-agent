# my-terraform-agent/importer/run.py

import os
import concurrent.futures
import threading
from . import config, gcp_client, terraform_client, hcl_generator, knowledge_base

# This block ensures that no matter where the script is run from,
# its "Current Working Directory" is always the main project folder.
project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
os.chdir(project_root)
print(f"--- Ensuring working directory is set to: {os.getcwd()} ---")

def _present_selection_menu(resources):
    """Presents a menu and accepts multiple, comma-separated inputs."""
    print("\n--- Stage 2: Select Resources to Import ---")
    for i, resource in enumerate(resources):
        display_name = resource.get('displayName', resource.get('name'))
        asset_type_short = resource.get('assetType').split('/')[-1]
        location = resource.get('location', 'N/A')
        print(f"  [{i + 1}] {display_name:<40} (Type: {asset_type_short:<10} | Location: {location})")
    
    while True:
        try:
            raw_input = input("\nEnter resource numbers separated by commas (e.g., 1, 5, 8), or 0 to cancel: ")
            if raw_input.strip() == '0': return []
            choices = [int(i.strip()) for i in raw_input.split(',')]
            selected_assets = [resources[c - 1] for c in choices if 1 <= c <= len(resources)]
            if selected_assets: return selected_assets
            else: print("❌ No valid selections made. Please try again.")
        except ValueError:
            print("❌ Invalid input. Please enter numbers separated by commas.")

def _map_asset_to_terraform(selected_asset, project_id):
    """Creates a mapping dictionary from asset details."""
    resource_name = selected_asset.get('displayName') or selected_asset['name'].split('/')[-1]
    asset_type = selected_asset['assetType']
    tf_type = config.ASSET_TO_TERRAFORM_MAP.get(asset_type)
    if not tf_type: return None
    print(f"   - Mapping asset '{resource_name}'...")
    return {
        "tf_type": tf_type, "hcl_name": resource_name.replace('-', '_'),
        "resource_name": resource_name, "import_id": selected_asset['name'].split('/', 2)[-1],
        "filename": f"{tf_type}_{resource_name.replace('-', '_')}.tf",
        "location": selected_asset.get('location'), "project_id": project_id,
    }

def _generate_and_save_hcl(mapping, schema):
    """Unit of work for initial HCL generation, now with definitive logging."""
    print(f"\n⚙️  Generating HCL for '{mapping['resource_name']}'...")
    resource_json = gcp_client.get_resource_details_json(mapping)
    if not resource_json:
        return (mapping, False, {"error": "Failed to get resource details."})

    generated_hcl = hcl_generator.generate_hcl_from_json(
        resource_json, mapping['tf_type'], mapping['hcl_name'], attempt=1, schema=schema
    )
    if not generated_hcl:
        return (mapping, False, {"error": "LLM failed to generate any HCL."})

    with open(mapping["filename"], "w", encoding='utf-8') as f:
        f.write(generated_hcl)
    print(f"✅ HCL file saved for '{mapping['resource_name']}'")

    # ---------------------------------------------------------------------------------
    # THIS IS THE NEW, CRITICAL DEBUG STEP
    # ---------------------------------------------------------------------------------
    print("\n   [DEBUG] Content written to file:")
    print("   --- START OF FILE CONTENT ---")
    print(generated_hcl)
    print("   --- END OF FILE CONTENT ---\n")
    # ---------------------------------------------------------------------------------

    # We now pass back the generated HCL and the original JSON for the correction loop
    return (mapping, True, {"hcl": generated_hcl, "json": resource_json})

def _attempt_correction(mapping, resource_json, previous_error, attempt_num, schema):
    """A unit of work for a single correction attempt in the retry loop."""
    corrected_hcl = hcl_generator.generate_hcl_from_json(
        resource_json, mapping['tf_type'], mapping['hcl_name'],
        attempt=attempt_num, previous_error=previous_error, schema=schema
    )
    if not corrected_hcl:
        return (mapping, "LLM failed to provide a correction.", False)

    with open(mapping["filename"], "w", encoding='utf-8') as f: f.write(corrected_hcl)
    is_success, plan_output = terraform_client.plan_for_resource(mapping['filename'])
    return (mapping, plan_output, is_success)

def run_workflow():
    """Main function with RAG-powered, parallel bulk import and self-correction."""
    print("🚀 Starting Google Cloud to Terraform Import Workflow...")
    if not os.path.isdir(".terraform"):
        if terraform_client.init() is None: return

    project_id = input("Enter your Google Cloud Project ID: ")
    
    print("\n--- Stage 1: Discovering All Supported Resources in Parallel ---")
    all_discovered_resources = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=config.MAX_DISCOVERY_WORKERS) as executor:
        future_to_asset_type = {
            executor.submit(gcp_client.discover_resources_of_type, project_id, asset_type): asset_type
            for asset_type in config.ASSET_TO_TERRAFORM_MAP
        }
        for future in concurrent.futures.as_completed(future_to_asset_type):
            asset_type = future_to_asset_type[future]
            try:
                resources = future.result()
                if resources: all_discovered_resources.extend(resources)
            except Exception as exc:
                print(f"❌ An error occurred during discovery for {asset_type}: {exc}")
    
    if not all_discovered_resources:
        print("\n🏁 No supported resources found."); return
    all_discovered_resources.sort(key=lambda r: r.get('displayName', r.get('name')))

    selected_assets = _present_selection_menu(all_discovered_resources)
    if not selected_assets: print("\nOperation cancelled."); return
    
    print("\n--- Stage 3: Mapping All Selected Assets ---")
    mappings = [m for m in [_map_asset_to_terraform(asset, project_id) for asset in selected_assets] if m is not None]
    if not mappings: print("❌ No valid resources to process."); return

    print("\n--- Pre-loading all required documentation schemas ---")
    schemas = {m['tf_type']: knowledge_base.get_schema_for_resource(m['tf_type']) for m in mappings}

    print("\n--- Generating Initial HCL Files in Parallel ---")
    initial_results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=config.MAX_IMPORT_WORKERS) as executor:
        future_to_gen = {executor.submit(_generate_and_save_hcl, m, schemas.get(m['tf_type'])): m for m in mappings}
        for future in concurrent.futures.as_completed(future_to_gen):
            mapping, success, data = future.result()
            initial_results.append({'mapping': mapping, 'is_success': success, 'data': data})

    successful_generations = [r for r in initial_results if r['is_success']]
    if not successful_generations:
        print("\n❌ HCL generation failed for all selected resources."); return

    terraform_client.init(upgrade=True)
    for result in successful_generations:
        terraform_client.import_resource(result['mapping'])

    print("\n--- Initial Verification of All Generated Files ---")
    all_results = []
    for result in successful_generations:
        is_success, output = terraform_client.plan_for_resource(result['mapping']['filename'])
        result['is_success'] = is_success
        if not is_success: result['data']['error'] = output
        all_results.append(result)

    successful_imports = [r for r in all_results if r['is_success']]
    failed_imports = [r for r in all_results if not r['is_success']]

    if failed_imports:
        print(f"\n--- Starting Self-Correction for {len(failed_imports)} Failed Resources ---")
        for i in range(config.MAX_LLM_RETRIES):
            if not failed_imports: print("\n✅ All resources corrected successfully!"); break
            print(f"\n--- Correction Cycle {i + 1} of {config.MAX_LLM_RETRIES} ---")
            corrections_in_progress = list(failed_imports)
            failed_imports.clear()
            
            with concurrent.futures.ThreadPoolExecutor(max_workers=config.MAX_IMPORT_WORKERS) as executor:
                future_to_corr = {
                    executor.submit(_attempt_correction, r['mapping'], r['data']['json'], r['data']['error'], i + 2, schemas.get(r['mapping']['tf_type'])): r['mapping']
                    for r in corrections_in_progress
                }
                for future in concurrent.futures.as_completed(future_to_corr):
                    mapping, output, is_success = future.result()
                    original_data = next((r['data'] for r in corrections_in_progress if r['mapping']['resource_name'] == mapping['resource_name']), {})
                    if is_success:
                        successful_imports.append({'mapping': mapping, 'data': original_data, 'is_success': True})
                    else:
                        failed_imports.append({'mapping': mapping, 'data': {'json': original_data.get('json'), 'error': output}, 'is_success': False})

    print("\n\n--- Bulk Import Complete ---")
    successful_imports.sort(key=lambda x: x['mapping']['resource_name'])
    failed_imports.sort(key=lambda x: x['mapping']['resource_name'])
    for result in successful_imports: print(f"✅ SUCCESS: {result['mapping']['resource_name']}")
    for result in failed_imports:
        error_line = result['data']['error'].splitlines()[0] if result['data'].get('error') else "Unknown error."
        print(f"❌ FAILED:  {result['mapping']['resource_name']} - {error_line}")
    
    print(f"\nSummary: {len(successful_imports)} / {len(mappings)} resources imported successfully.")
    print("Workflow finished.")

if __name__ == "__main__":
    run_workflow()