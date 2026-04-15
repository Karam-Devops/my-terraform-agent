# my-terraform-agent/importer/run.py

import os
import sys
import json
import concurrent.futures
import threading
import time
import re
from . import config, gcp_client, terraform_client, hcl_generator, knowledge_base, heuristics

project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
os.chdir(project_root)
print(f"--- Ensuring working directory is set to: {os.getcwd()} ---")

# --- UTILITIES ---
def _clean_terraform_output(raw_output):
    """Strips out Terraform's 'Refreshing state...' noise."""
    lines = raw_output.splitlines()
    clean_lines = [line.strip(' \t│╷╵') for line in lines if "Refreshing state..." not in line and "Reading..." not in line and "Read complete" not in line and line.strip(' \t│╷╵')]
    return "\n".join(clean_lines)

def snake_to_camel(snake_str):
    components = snake_str.split('_')
    if not components: return snake_str
    return components[0] + ''.join(x.title() for x in components[1:])

def remove_key_recursively(obj, key_to_remove):
    if isinstance(obj, dict):
        if key_to_remove in obj: del obj[key_to_remove]
        for k, v in list(obj.items()): remove_key_recursively(v, key_to_remove)
    elif isinstance(obj, list):
        for item in obj: remove_key_recursively(item, key_to_remove)

def scrub_json(json_str, keys_to_omit):
    """Scrubs specific keys AND automatically removes empty lists/dicts."""
    try:
        data = json.loads(json_str)
        for k in keys_to_omit:
            remove_key_recursively(data, k)
            remove_key_recursively(data, snake_to_camel(k))
            
        keys_to_delete = [k for k, v in data.items() if (isinstance(v, list) and not v) or (isinstance(v, dict) and not v)]
        for k in keys_to_delete:
            del data[k]
            print(f"   - 🧹 Auto-scrubbed empty top-level field: '{k}'")

        return json.dumps(data, indent=2)
    except Exception as e:
        print(f"   ⚠️ Could not scrub JSON: {e}")
        return json_str

def _scrub_hcl(hcl_str, keys_to_omit):
    """Physically removes lines from HCL that assign values to omitted keys."""
    if not keys_to_omit: return hcl_str
    lines = hcl_str.splitlines()
    clean_lines = []
    for line in lines:
        should_omit = False
        for key in keys_to_omit:
            pattern = rf"^\s*{re.escape(key)}\s*="
            if re.search(pattern, line):
                print(f"   - 🛡️  HCL SCRUBBER: Deleted hallucinated line: '{line.strip()}'")
                should_omit = True
                break
        if not should_omit: clean_lines.append(line)
    return "\n".join(clean_lines)

def get_multiline_input():
    print("\nPaste your correct HCL snippet below.")
    print("OR use a Surgical Command:")
    print("  OMIT:field_name   (For fields causing syntax errors)")
    print("  IGNORE:field_name (For computed fields causing replacement diffs)")
    print("On a new line, press Ctrl+Z and then Enter (Windows) or Ctrl+D (Unix) to submit.")
    print("---------------------------------------------------------------------------------")
    lines = sys.stdin.readlines()
    return "".join(lines) if lines else ""

# --- CORE LOGIC ---
def _present_selection_menu(resources):
    print("\n--- Stage 2: Select Resources to Import ---")
    for i, resource in enumerate(resources):
        display_name = resource.get('displayName', resource.get('name'))
        asset_type_short = resource.get('assetType').split('/')[-1]
        print(f"  [{i + 1}] {display_name:<40} (Type: {asset_type_short:<10})")
    
    while True:
        try:
            raw_input = input("\nEnter resource numbers separated by commas (e.g., 1, 5), or 0 to cancel: ")
            if raw_input.strip() == '0': return []
            choices = [int(i.strip()) for i in raw_input.split(',')]
            selected_assets = [resources[c - 1] for c in choices if 1 <= c <= len(resources)]
            if selected_assets: return selected_assets
            else: print("❌ No valid selections made.")
        except ValueError: print("❌ Invalid input.")

def _map_asset_to_terraform(selected_asset, project_id):
    resource_name = selected_asset.get('displayName') or selected_asset['name'].split('/')[-1]
    asset_type = selected_asset['assetType']
    tf_type = config.ASSET_TO_TERRAFORM_MAP.get(asset_type)
    if not tf_type: return None
    
    info = config.TF_TYPE_TO_GCLOUD_INFO.get(tf_type)
    import_id_format = info.get("import_id_format") if info else None
    
    if not import_id_format:
        import_id = selected_asset['name'].split('/', 2)[-1]
    else:
        parts = selected_asset['name'].split('/')
        format_vars = {
            'project': project_id, 'name': resource_name,
            'zone': selected_asset.get('location'), 'region': selected_asset.get('location'),
        }
        if tf_type == 'google_service_account': format_vars['email'] = selected_asset.get('displayName')
        if tf_type == 'google_container_node_pool' and 'clusters' in parts:
             format_vars['cluster'] = parts[parts.index('clusters') + 1]
        import_id = import_id_format.format(**format_vars)

    return {
        "tf_type": tf_type, "hcl_name": resource_name.replace('-', '_'),
        "resource_name": resource_name, "import_id": import_id,
        "filename": f"{tf_type}_{resource_name.replace('-', '_')}.tf",
        "location": selected_asset.get('location'), "project_id": project_id,
    }

def _generate_and_save_hcl(mapping, schema, heuristics_kb):
    """Initial HCL generation (Attempt 1)."""
    print(f"\n⚙️  Generating HCL for '{mapping['resource_name']}'...")
    resource_json = gcp_client.get_resource_details_json(mapping)
    if not resource_json: return (mapping, False, {"error": "Failed to get resource details."})

    keys_to_omit, fields_to_ignore = [], []
    if mapping['tf_type'] in heuristics_kb:
        for error_key, snippet in heuristics_kb[mapping['tf_type']].items():
            cmd = snippet.strip().upper()
            if cmd == "OMIT": keys_to_omit.append(error_key)
            elif cmd.startswith("IGNORE"):
                fields_to_ignore.append(cmd.split(":", 1)[1].strip() if ":" in cmd else error_key)

    if keys_to_omit:
        print(f"   - 🛡️  Proactively scrubbing JSON keys: {keys_to_omit}")
        resource_json = scrub_json(resource_json, keys_to_omit)

    generated_hcl = hcl_generator.generate_hcl_from_json(
        resource_json, mapping['tf_type'], mapping['hcl_name'], attempt=1, schema=schema,
        expert_snippet=None, keys_to_omit=keys_to_omit, fields_to_ignore=fields_to_ignore
    )
    
    if not generated_hcl: return (mapping, False, {"error": "LLM failed to generate HCL."})

    generated_hcl = _scrub_hcl(generated_hcl, keys_to_omit)

    try:
        with open(mapping["filename"], "w", encoding='utf-8-sig') as f: f.write(generated_hcl)
        print(f"✅ HCL file saved for '{mapping['resource_name']}'")
        time.sleep(1)
    except IOError as e:
        return (mapping, False, {"error": f"File write error: {e}"})
    
    return (mapping, True, {"hcl": generated_hcl, "json": resource_json})

def _attempt_correction(mapping, resource_json, previous_error, attempt_num, schema, heuristics_kb, manual_snippet=None, manual_trigger_key=None):
    """A unit of work for correction attempts (Automated or Interactive)."""
    keys_to_omit, fields_to_ignore, expert_snippet = [], [], None
    
    # 1. Load global memory
    if mapping['tf_type'] in heuristics_kb:
        for error_key, snippet in heuristics_kb[mapping['tf_type']].items():
            cmd = snippet.strip().upper()
            if cmd == "OMIT": keys_to_omit.append(error_key)
            elif cmd.startswith("IGNORE"):
                fields_to_ignore.append(cmd.split(":", 1)[1].strip() if ":" in cmd else error_key)

    # 2. Handle DB Snippet lookup (for Automated Retry)
    error_signature = manual_trigger_key or heuristics.generate_error_signature(previous_error, mapping['tf_type'])
    
    if not manual_snippet:
        db_snippet = heuristics.get_heuristic_for_error(mapping['tf_type'], error_signature)
        if db_snippet:
            cmd = db_snippet.strip().upper()
            if cmd == "OMIT":
                if error_signature not in keys_to_omit: keys_to_omit.append(error_signature)
            elif cmd.startswith("IGNORE"):
                field = cmd.split(":", 1)[1].strip() if ":" in cmd else error_signature
                if field not in fields_to_ignore: fields_to_ignore.append(field)
            else:
                expert_snippet = db_snippet # It's raw code

    # 3. Handle Manual Input (Overrides DB)
    if manual_snippet:
        cmd = manual_snippet.strip().upper()
        if cmd.startswith("OMIT"):
            field = cmd.split(":", 1)[1].strip() if ":" in cmd else error_signature
            if field not in keys_to_omit: keys_to_omit.append(field)
        elif cmd.startswith("IGNORE"):
            field = cmd.split(":", 1)[1].strip() if ":" in cmd else error_signature
            if field not in fields_to_ignore: fields_to_ignore.append(field)
        else:
            expert_snippet = manual_snippet # It's raw code

    if keys_to_omit:
        print(f"   - 🛡️  Scrubbing JSON keys for correction: {keys_to_omit}")
        resource_json = scrub_json(resource_json, keys_to_omit)

    corrected_hcl = hcl_generator.generate_hcl_from_json(
        resource_json, mapping['tf_type'], mapping['hcl_name'], attempt=attempt_num, 
        previous_error=previous_error, schema=schema, 
        expert_snippet=expert_snippet, keys_to_omit=keys_to_omit, fields_to_ignore=fields_to_ignore
    )
    
    if not corrected_hcl: return (mapping, "LLM failed to provide a correction.", False)

    corrected_hcl = _scrub_hcl(corrected_hcl, keys_to_omit)

    with open(mapping["filename"], "w", encoding='utf-8-sig') as f: f.write(corrected_hcl)
    print(f"✅ Saved corrected HCL for '{mapping['resource_name']}'.")
    time.sleep(1)

    print(f"   - Re-attempting import for '{mapping['resource_name']}'...")
    terraform_client.import_resource(mapping, force_refresh=True)

    is_success, plan_output = terraform_client.plan_for_resource(mapping['filename'])
    if is_success and error_signature != "generic_error" and not manual_snippet:
        heuristics.save_heuristic(mapping['tf_type'], error_signature, corrected_hcl)

    # --- THIS IS THE FIX FOR THE REPORTING BUG ---
    # Return the new plan_output so the main loop updates its record of the current state
    return (mapping, plan_output, is_success)

def run_workflow():
    print("🚀 Starting Google Cloud to Terraform Import Workflow...")
    if not os.path.isdir(".terraform"):
        if terraform_client.init() is None: return

    project_id = input("Enter your Google Cloud Project ID: ")
    os.environ["GOOGLE_PROJECT"] = project_id.strip()

    print("\n--- Stage 1: Discovering All Supported Resources in Parallel ---")
    all_discovered_resources = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=config.MAX_DISCOVERY_WORKERS) as executor:
        future_to_asset_type = {executor.submit(gcp_client.discover_resources_of_type, project_id, at): at for at in config.ASSET_TO_TERRAFORM_MAP}
        for future in concurrent.futures.as_completed(future_to_asset_type):
            try:
                resources = future.result()
                if resources: all_discovered_resources.extend(resources)
            except Exception as exc: print(f"❌ Error during discovery: {exc}")
    
    if not all_discovered_resources: return
    all_discovered_resources.sort(key=lambda r: r.get('displayName', r.get('name')))

    selected_assets = _present_selection_menu(all_discovered_resources)
    if not selected_assets: return
    
    print("\n--- Stage 3: Mapping All Selected Assets ---")
    mappings = [m for m in [_map_asset_to_terraform(asset, project_id) for asset in selected_assets] if m is not None]
    if not mappings: return

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
    if not successful_generations: return

    terraform_client.init(upgrade=True)
    for result in successful_generations: terraform_client.import_resource(result['mapping'])

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
        print("\n" + "="*70)
        print(f"--- Starting Interactive Correction for {len(failed_imports)} Failed Resources ---")
        print("="*70)
        
        for failed_item in list(failed_imports):
            mapping = failed_item['mapping']
            current_error = failed_item['data']['error']
            resource_json = failed_item['data']['json']
            
            while True:
                clean_error = _clean_terraform_output(current_error)
                print(f"\n🛑 RESOURCE: '{mapping['resource_name']}'\n--- TERRAFORM DIFF / ERROR ---")
                error_lines = clean_error.splitlines()
                for line in error_lines[:25]: print(f"  {line}")
                if len(error_lines) > 25: print("  ... (output truncated) ...")
                print("-" * 70)

                choice = input("Choose an action:\n  [1] Provide a snippet or OMIT/IGNORE command.\n  [2] Let AI self-correct.\n  [3] Skip resource.\nEnter your choice: ").strip()
                if choice not in ['1', '2', '3']: print("Invalid choice."); continue
                if choice == '3': break

                if choice == '1': 
                    error_trigger_key = heuristics.generate_error_signature(clean_error, mapping['tf_type'])
                    print(f"\n--- Teaching Mode for error pattern: '{error_trigger_key}' ---")
                    user_input = get_multiline_input().strip()
                    if not user_input: continue
                    
                    if user_input.upper().startswith("OMIT:") or user_input.upper().startswith("IGNORE:"):
                        try:
                            cmd, field = user_input.split(":", 1)
                            error_trigger_key = field.strip()
                            user_input = cmd.upper()
                        except ValueError:
                            print("❌ Invalid syntax. Use OMIT:field or IGNORE:field"); continue
                            
                    heuristics.save_heuristic(mapping['tf_type'], error_trigger_key, user_input)
                    
                    mapping, plan_output, is_success = _attempt_correction(
                        mapping, resource_json, clean_error, 2, schemas.get(mapping['tf_type']), 
                        heuristics.load_heuristics(), manual_snippet=user_input, manual_trigger_key=error_trigger_key
                    )
                    
                    if is_success:
                        print(f"✅ Correction SUCCEEDED for '{mapping['resource_name']}'!")
                        successful_imports.append(failed_item)
                        failed_imports.remove(failed_item)
                        break
                    else:
                        print("❌ Correction failed. Returning to menu.")
                        failed_item['data']['error'] = plan_output

                if choice == '2':
                    for i in range(config.MAX_LLM_RETRIES):
                        print(f"--- AI Cycle {i + 1} of {config.MAX_LLM_RETRIES} ---")
                        mapping, output, is_success = _attempt_correction(
                            mapping, resource_json, current_error, i + 2, schemas.get(mapping['tf_type']), heuristics.load_heuristics()
                        )
                        if is_success:
                            print(f"✅ AI SUCCEEDED on attempt {i + 1}!")
                            successful_imports.append(failed_item)
                            failed_imports.remove(failed_item)
                            break 
                        else:
                            current_error = output 

                    if not is_success: print(f"❌ AI FAILED after {config.MAX_LLM_RETRIES} retries.")

    print("\n\n--- Bulk Import Complete ---")
    successful_imports.sort(key=lambda x: x['mapping']['resource_name'])
    failed_imports.sort(key=lambda x: x['mapping']['resource_name'])
    for result in successful_imports: print(f"✅ SUCCESS: {result['mapping']['resource_name']}")
    for result in failed_imports:
        clean_err = _clean_terraform_output(result['data']['error'])
        first_line = clean_err.splitlines()[0] if clean_err else "Unknown error."
        print(f"❌ FAILED:  {result['mapping']['resource_name']} - {first_line}")
    print(f"\nSummary: {len(successful_imports)} / {len(mappings)} resources imported successfully.")

if __name__ == "__main__":
    run_workflow()