# my-terraform-agent/importer/run.py

import os
import concurrent.futures
import sys
import threading
from . import config, gcp_client, terraform_client, hcl_generator, knowledge_base, heuristics

# Set working directory to project root
project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
os.chdir(project_root)
print(f"--- Ensuring working directory is set to: {os.getcwd()} ---")

def get_multiline_input():
    """Gets multi-line input from the user with clear instructions."""
    print("\nPaste your correct HCL snippet below.")
    print("On a new line, press Ctrl+Z and then Enter (Windows) or Ctrl+D (Unix) to submit.")
    print("---------------------------------------------------------------------------------")
    lines = sys.stdin.readlines()
    if not lines: return ""
    return "".join(lines)

def _present_selection_menu(resources):
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

def _generate_and_save_hcl(mapping, schema, heuristics_kb):
    print(f"\n⚙️  Generating HCL for '{mapping['resource_name']}'...")
    resource_json = gcp_client.get_resource_details_json(mapping)
    if not resource_json:
        return (mapping, False, {"error": "Failed to get resource details."})

    expert_example = None
    if mapping['tf_type'] in heuristics_kb:
        expert_example = next(iter(heuristics_kb[mapping['tf_type']].values()), None)

    generated_hcl = hcl_generator.generate_hcl_from_json(
        resource_json, mapping['tf_type'], mapping['hcl_name'], 
        attempt=1, schema=schema, expert_example=expert_example
    )
    if not generated_hcl:
        return (mapping, False, {"error": "LLM failed to generate HCL."})

    with open(mapping["filename"], "w", encoding='utf-8') as f:
        f.write(generated_hcl)
    print(f"✅ HCL file saved for '{mapping['resource_name']}'")
    
    return (mapping, True, {"hcl": generated_hcl, "json": resource_json})

def _attempt_correction(mapping, resource_json, previous_error, attempt_num, schema, heuristics_kb):
    """A unit of work for a single correction attempt in the AI retry loop."""
    error_signature = heuristics.generate_error_signature(previous_error, mapping['tf_type'])
    expert_example = heuristics.get_heuristic_for_error(mapping['tf_type'], error_signature)

    corrected_hcl = hcl_generator.generate_hcl_from_json(
        resource_json, mapping['tf_type'], mapping['hcl_name'],
        attempt=attempt_num, previous_error=previous_error, schema=schema, expert_example=expert_example
    )
    if not corrected_hcl:
        return (mapping, "LLM failed to provide a correction.", False)

    with open(mapping["filename"], "w", encoding='utf-8') as f:
        f.write(corrected_hcl)
    print(f"✅ Saved corrected HCL for '{mapping['resource_name']}'.")

    # --- NEW: Re-run import because the HCL changed ---
    print(f"   - Re-attempting import for '{mapping['resource_name']}'...")
    terraform_client.import_resource(mapping)

    is_success, plan_output = terraform_client.plan_for_resource(mapping['filename'])
    if is_success:
        heuristics.save_heuristic(mapping['tf_type'], error_signature, corrected_hcl)

    return (mapping, plan_output, is_success)

def run_workflow():
    print("🚀 Starting Google Cloud to Terraform Import Workflow...")
    if not os.path.isdir(".terraform"):
        if terraform_client.init() is None: return

    project_id = input("Enter your Google Cloud Project ID: ")
    # Tell Terraform to use this project globally by setting its environment variable.
    os.environ["GOOGLE_PROJECT"] = project_id.strip()
    # --- END OF FIX ---
    
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
        print("\n🏁 No supported resources found in the project. Workflow finished."); return
        
    all_discovered_resources.sort(key=lambda r: r.get('displayName', r.get('name')))

    selected_assets = _present_selection_menu(all_discovered_resources)
    if not selected_assets: print("\nOperation cancelled by user."); return
    
    print("\n--- Stage 3: Mapping All Selected Assets ---")
    mappings = [m for m in [_map_asset_to_terraform(asset, project_id) for asset in selected_assets] if m is not None]
    if not mappings: print("❌ No valid resources to process after mapping. Aborting."); return

    print("\n--- Pre-loading all required documentation schemas and heuristics ---")
    schemas = {m['tf_type']: knowledge_base.get_schema_for_resource(m['tf_type']) for m in mappings}
    heuristics_kb = heuristics.load_heuristics()

    print("\n--- Generating Initial HCL Files in Parallel ---")
    initial_results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=config.MAX_IMPORT_WORKERS) as executor:
        future_to_gen = {executor.submit(_generate_and_save_hcl, m, schemas.get(m['tf_type']), heuristics_kb): m for m in mappings}
        for future in concurrent.futures.as_completed(future_to_gen):
            mapping, success, data = future.result()
            initial_results.append({'mapping': mapping, 'is_success': success, 'data': data})

    successful_generations = [r for r in initial_results if r['is_success']]
    if not successful_generations:
        print("\n❌ HCL generation failed for all selected resources. Aborting."); return

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

    # --- Interactive Correction Loop ---
    if failed_imports:
        print("\n" + "="*50)
        print(f"--- Starting Interactive Correction for {len(failed_imports)} Failed Resources ---")
        print("="*50)
        
        for failed_item in list(failed_imports):
            mapping = failed_item['mapping']
            error = failed_item['data']['error']
            resource_json = failed_item['data']['json']
            
            print(f"\nResource: '{mapping['resource_name']}'")
            print("Error Details:")
            # --- THE FIX: Print the first several lines of the actual error to give context ---
            error_lines = [line for line in error.splitlines() if line.strip() and not line.strip() in ['╷', '╵', '│']]
            for line in error_lines[:10]: # Print up to 10 lines of the error
                print(f"  {line}")
            print("-" * 40)

            while True:
                choice = input(
                    "Choose an action:\n"
                    "  [1] Provide a correct HCL snippet to teach the agent (Recommended).\n"
                    "  [2] Let the AI attempt to self-correct (up to 5 retries).\n"
                    "  [3] Skip this resource and continue.\n"
                    "Enter your choice: "
                ).strip()
                if choice in ['1', '2', '3']: break
                print("Invalid choice. Please enter 1, 2, or 3.")

            if choice == '3':
                print(f"Skipping '{mapping['resource_name']}'.")
                continue

            if choice == '1': 
                error_trigger_key = heuristics.generate_error_signature(error, mapping['tf_type'])
                print(f"\n--- Teaching Mode for error pattern: '{error_trigger_key}' ---")
                
                correct_snippet = get_multiline_input()
                if not correct_snippet.strip():
                    print("No snippet provided. Skipping resource."); continue
                
                heuristics.save_heuristic(mapping['tf_type'], error_trigger_key, correct_snippet)
                
                print("--- Retrying with newly learned heuristic... ---")
                corrected_hcl = hcl_generator.generate_hcl_from_json(
                    resource_json, mapping['tf_type'], mapping['hcl_name'], 
                    attempt=2, schema=schemas.get(mapping['tf_type']), expert_example=correct_snippet
                )
                if corrected_hcl:
                    with open(mapping["filename"], "w", encoding='utf-8') as f: f.write(corrected_hcl)
                    
                    # --- NEW: Re-run import because the HCL changed ---
                    print("   - Re-attempting import with corrected HCL...")
                    terraform_client.import_resource(mapping)
                    
                    is_success, _ = terraform_client.plan_for_resource(mapping['filename'])
                    if is_success:
                        print(f"✅ Human-in-the-Loop Correction SUCCEEDED for '{mapping['resource_name']}'!")
                        successful_imports.append(failed_item)
                        failed_imports.remove(failed_item)
                continue

            if choice == '2':
                print("--- AI self-correction loop initiated... ---")
                temp_failed_item = failed_item
                for i in range(config.MAX_LLM_RETRIES):
                    print(f"--- Correction Cycle {i + 1} of {config.MAX_LLM_RETRIES} for '{mapping['resource_name']}' ---")
                    expert_example = heuristics.get_heuristic_for_error(mapping['tf_type'], heuristics.generate_error_signature(temp_failed_item['data']['error'], mapping['tf_type']))
                    
                    # Call the corrected unit of work
                    mapping, output, is_success = _attempt_correction(
                        mapping, resource_json, temp_failed_item['data']['error'], i + 2, schemas.get(mapping['tf_type']), heuristics_kb
                    )

                    if is_success:
                        print(f"✅ AI Self-Correction SUCCEEDED for '{mapping['resource_name']}' on attempt {i + 1}!")
                        successful_imports.append(failed_item)
                        failed_imports.remove(failed_item)
                        break 
                    else:
                        temp_failed_item['data']['error'] = output 

                if not is_success:
                    print(f"❌ AI Self-Correction FAILED for '{mapping['resource_name']}' after {config.MAX_LLM_RETRIES} retries.")

    print("\n\n--- Bulk Import Complete ---")
    successful_imports.sort(key=lambda x: x['mapping']['resource_name'])
    failed_imports.sort(key=lambda x: x['mapping']['resource_name'])
    for result in successful_imports:
        print(f"✅ SUCCESS: {result['mapping']['resource_name']}")
    for result in failed_imports:
        clean_lines = [line.strip(' \t│╷╵') for line in result['data']['error'].splitlines() if line.strip(' \t│╷╵')]
        first_line = clean_lines[0] if clean_lines else "Unknown error."
        print(f"❌ FAILED:  {result['mapping']['resource_name']} - {first_line}")
    
    print(f"\nSummary: {len(successful_imports)} / {len(mappings)} resources imported successfully.")
    print("Workflow finished.")

if __name__ == "__main__":
    run_workflow()