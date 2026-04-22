# my-terraform-agent/importer/run.py

import os
import sys
import json
import concurrent.futures
import threading
import time
import re
from . import (
    config, gcp_client, terraform_client, hcl_generator, knowledge_base,
    heuristics, snapshot_scrubber, lifecycle_planner, resource_mode,
)

project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
os.chdir(project_root)
print(f"--- Ensuring working directory is set to: {os.getcwd()} ---")

def _clean_terraform_output(raw_output):
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

def _classify_blockage(failed_item):
    """Classify a failed item as SELF_BROKEN or BLOCKED_BY_SIBLING.

    Path 1: when one resource's .tf file has a config-load error
    (`Unsupported argument`, `Unsupported block type`), every other
    resource's `-target` plan fails with the SAME error pointing at the
    broken sibling. We don't want the menu to show 3 copies of the same
    cluster error against bucket / GCE / GKE names — the operator skips
    all three and we report "0 / 3 succeeded" when the imports actually
    worked.

    Returns ('self_broken', None) or ('blocked', blocker_filename).

    Heuristic:
      * If the error mentions THIS resource's own .tf file at all, it's
        SELF_BROKEN — the file is at least partially the problem and the
        operator needs to look at it.
      * If the error only mentions OTHER .tf files, it's BLOCKED — fix
        the sibling first; this resource may auto-resolve on re-verify.
      * If no file is mentioned (no `on X.tf line N` marker), default to
        SELF_BROKEN — safe fallback so we don't suppress real errors.
    """
    own_file = failed_item['mapping']['filename']
    error_text = failed_item['data'].get('error', '')
    files_in_error = terraform_client.extract_error_files(error_text)
    if not files_in_error:
        return ('self_broken', None)
    if own_file in files_in_error:
        return ('self_broken', None)
    # Filter to siblings that are actually different from our own file
    siblings = [f for f in files_in_error if f != own_file]
    if not siblings:
        return ('self_broken', None)
    return ('blocked', siblings[0])


def _annotate_blockage(failed_imports):
    """Tag every failed_item with `_blockage` and `_blocker` in place."""
    for item in failed_imports:
        kind, blocker = _classify_blockage(item)
        item['_blockage'] = kind
        item['_blocker'] = blocker


def _refresh_blocked_after_fix(failed_imports, successful_imports, just_fixed_filename):
    """Re-verify blocked items after a sibling has been fixed.

    Path 1: when SELF_BROKEN item X's .tf file gets fixed, any BLOCKED
    item whose `_blocker == X.filename` may now plan cleanly. Re-run
    `plan_for_resource` for each, promote PASSes to successful_imports,
    and reclassify items still failing.

    We only refresh items whose blocker filename matches what was just
    fixed — refreshing every blocked item after every fix is wasteful
    when typical demos have 3-10 blocked siblings.
    """
    promoted = []
    for item in list(failed_imports):
        if item.get('_blockage') != 'blocked':
            continue
        if item.get('_blocker') != just_fixed_filename:
            continue
        mapping = item['mapping']
        print(f"\n   - 🔄 Re-verifying '{mapping['resource_name']}' "
              f"(was blocked by {just_fixed_filename}, now fixed)...")
        is_success, plan_output = terraform_client.plan_for_resource(mapping)
        if is_success:
            print(f"   - 🟢 UNBLOCKED & PASSED: '{mapping['resource_name']}' "
                  f"auto-promoted to successful imports.")
            item['is_success'] = True
            item['data']['error'] = ''
            successful_imports.append(item)
            failed_imports.remove(item)
            promoted.append(item)
        else:
            # Still failing — update error text and reclassify so the
            # menu shows the current state (might now be self-broken if
            # the original blocker hid an own-file issue).
            item['data']['error'] = plan_output
            kind, new_blocker = _classify_blockage(item)
            item['_blockage'] = kind
            item['_blocker'] = new_blocker
            tag = f"{kind}" + (f" (now blocked by {new_blocker})" if new_blocker else "")
            print(f"   - ⚠️  '{mapping['resource_name']}' still failing — "
                  f"reclassified as {tag}")
    return promoted


def get_multiline_input():
    print("\nPaste your correct HCL snippet below.")
    print("OR use a Surgical Command:")
    print("  OMIT:field_name    (For fields causing syntax errors)")
    print("  IGNORE:field_name  (For computed fields causing replacement diffs)")
    print("  SNIPPET:field_name (To save the following snippet under a specific key)")
    print("On a new line, press Ctrl+Z and then Enter (Windows) or Ctrl+D (Unix) to submit.")
    print("---------------------------------------------------------------------------------")
    lines = sys.stdin.readlines()
    return "".join(lines) if lines else ""

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
    """Initial HCL generation (Attempt 1) - FIXED PROACTIVE MEMORY"""
    print(f"\n⚙️  Generating HCL for '{mapping['resource_name']}'...")
    resource_json = gcp_client.get_resource_details_json(mapping)
    if not resource_json: return (mapping, False, {"error": "Failed to get resource details."})

    # --- 0. PR-3: schema-driven auto-scrub of pure-computed fields ---
    # Removes provider-set, read-only attributes (terraform_labels, self_link,
    # creationTimestamp, etc.) before any LLM sees the JSON. Path-aware so a
    # nested `name` is not confused with a top-level instance `name`. This is
    # what `heuristics.json` was patching by hand for the computed class of
    # bugs.
    resource_json, auto_stripped = snapshot_scrubber.auto_scrub_cloud_snapshot(
        resource_json, mapping['tf_type']
    )
    if auto_stripped:
        print(f"   - 🛡️  Auto-scrubbed {len(auto_stripped)} computed-only field(s) "
              f"from cloud snapshot:")
        for p in auto_stripped:
            print(f"       - {p}")

    # --- 0b. PR-6: drop GCP-managed labels (goog-*, gke-*, k8s-io-*) ---
    # These leak into `labels` from `gcloud describe` and create perpetual
    # plan diffs because the provider also reports them as service-managed.
    # Stripping at snapshot stage means the LLM never writes them.
    resource_json, dropped_labels = snapshot_scrubber.filter_auto_labels(resource_json)
    if dropped_labels:
        print(f"   - 🏷️  Stripped {len(dropped_labels)} provider-managed label(s):")
        for p in dropped_labels:
            print(f"       - {p}")

    # --- 0b-2. PR-11: strip provider-dropped paths (API still returns, schema doesn't) ---
    # GCP APIs keep echoing fields that the current google TF provider has
    # removed support for (e.g. the retired GKE `kubernetes_dashboard` addon).
    # Writing them to HCL produces `Unsupported block type` / `Unsupported
    # argument` at plan time. Strip at snapshot stage before the LLM sees it.
    resource_json, dropped_provider_paths = snapshot_scrubber.filter_provider_dropped_paths(
        resource_json
    )
    if dropped_provider_paths:
        print(f"   - 🧽 Stripped {len(dropped_provider_paths)} provider-dropped path(s):")
        for p in dropped_provider_paths:
            print(f"       - {p}")

    # --- 0c. PR-10: resource-mode detection + pruning ---
    # Some resources have runtime modes (GKE Autopilot, etc.) that forbid
    # large schema sub-trees the per-attribute oracle still lists as
    # OPTIONAL. Detect the mode from the snapshot, prune the forbidden
    # cloud-side blocks, and remember the mode so we can inject a
    # high-priority instruction into the LLM prompt below.
    active_modes: list = []
    mode_addendum: str = ""
    try:
        _data_for_modes = json.loads(resource_json)
        active_modes = resource_mode.detect_modes(_data_for_modes, mapping['tf_type'])
        if active_modes:
            print(f"   - 🧭 Detected resource mode(s): {active_modes}")
            _data_for_modes, dropped_mode_keys = resource_mode.apply_modes(
                _data_for_modes, active_modes
            )
            if dropped_mode_keys:
                print(f"   - 🧹 Mode-pruned {len(dropped_mode_keys)} top-level key(s) "
                      f"from snapshot:")
                for k in dropped_mode_keys:
                    print(f"       - {k}")
                resource_json = json.dumps(_data_for_modes, indent=2)
            mode_addendum = resource_mode.mode_prompt_addendum(active_modes)
    except (json.JSONDecodeError, TypeError) as _e:
        print(f"   - WARN: mode detection skipped (snapshot parse error: {_e})")

    # --- 0d. PR-12: drop top-level keys that collapsed to {} / [] after prunes ---
    # The LLM emits `block {}` for any key it sees in the JSON; the provider
    # rejects empty blocks whose inner fields are required (classic case:
    # `maintenance_policy {}` requires one of `daily_maintenance_window` /
    # `recurring_window`). Has to run after every prune pass above.
    resource_json, dropped_empty = snapshot_scrubber.drop_empty_top_level_keys(
        resource_json
    )
    if dropped_empty:
        print(f"   - 🫥 Dropped {len(dropped_empty)} empty top-level key(s) "
              f"after prune passes:")
        for k in dropped_empty:
            print(f"       - {k}")

    keys_to_omit = []
    fields_to_ignore = []
    expert_snippets = [] 

    # --- 1. LOAD ALL MEMORY ---
    if mapping['tf_type'] in heuristics_kb:
        for error_key, snippet in heuristics_kb[mapping['tf_type']].items():
            heuristics.warn_legacy_rule_used(mapping['tf_type'], error_key, snippet)
            cmd = snippet.strip().upper()
            if cmd == "OMIT":
                keys_to_omit.append(error_key)
            elif cmd.startswith("IGNORE"):
                fields_to_ignore.append(cmd.split(":", 1)[1].strip() if ":" in cmd else error_key)
            else:
                expert_snippets.append(snippet) # It's a code block

    # --- 1b. PR-4: schema-derived lifecycle.ignore_changes ---
    # Top-level optional+computed attributes that the cloud actually returned
    # a value for. Adding them to ignore_changes makes future provider-side
    # recomputes silent — captures import-time value, suppresses drift.
    try:
        cloud_for_planner = json.loads(resource_json)
    except (json.JSONDecodeError, TypeError):
        cloud_for_planner = {}
    auto_ignore = lifecycle_planner.derive_lifecycle_ignores(
        cloud_for_planner, mapping['tf_type']
    )
    if auto_ignore:
        new_ignore = [f for f in auto_ignore if f not in fields_to_ignore]
        if new_ignore:
            print(f"   - 🔒 Auto-derived lifecycle.ignore_changes for "
                  f"{len(new_ignore)} optional+computed field(s):")
            for f in new_ignore:
                print(f"       - {f}")
            fields_to_ignore.extend(new_ignore)

    # --- 1c. PR-7 sanity gate: never let pure-computed paths into ignore_changes ---
    # Terraform errors on `ignore_changes = [terraform_labels]` because there's
    # no configured value to ignore. Legacy heuristics.json rules sometimes
    # IGNORE'd such fields; drop them here regardless of source.
    try:
        from . import schema_oracle as _so
        _oracle = _so.get_oracle()
        if _oracle.has(mapping['tf_type']):
            _pure = set(_oracle.computed_only_paths(mapping['tf_type']))
            _bad = [f for f in fields_to_ignore if f in _pure]
            if _bad:
                print(f"   - ⚠️  Dropping {len(_bad)} pure-computed name(s) from "
                      f"ignore_changes (Terraform rejects these): {_bad}")
                fields_to_ignore = [f for f in fields_to_ignore if f not in _pure]
    except Exception as _e:  # noqa: BLE001 - fail open
        print(f"   - WARN: pure-computed sanity gate skipped ({_e})")

    # --- 2. PROACTIVE JSON SCRUBBING ---
    if keys_to_omit:
        print(f"   - 🛡️  Proactively scrubbing JSON keys: {keys_to_omit}")
        resource_json = scrub_json(resource_json, keys_to_omit)

    # --- 3. BUILD EXPERT INSTRUCTIONS ---
    expert_instructions = []
    if fields_to_ignore:
        expert_instructions.append("IGNORE_LIST:" + ",".join(fields_to_ignore))
    if expert_snippets:
        expert_instructions.append("\n".join(expert_snippets))
    
    expert_snippet_str = "\n".join(expert_instructions) if expert_instructions else None

    # --- 4. CALL LLM WITH ALL CONTEXT ---
    generated_hcl = hcl_generator.generate_hcl_from_json(
        resource_json, mapping['tf_type'], mapping['hcl_name'],
        attempt=1, schema=schema, expert_snippet=expert_snippet_str,
        keys_to_omit=keys_to_omit, mode_addendum=mode_addendum,
    )
    
    if not generated_hcl: return (mapping, False, {"error": "LLM failed to generate HCL."})

    # --- 5. PROACTIVE HCL SCRUBBING ---
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
    keys_to_omit = []
    fields_to_ignore = []
    expert_snippets = []
    
    # 1. Load global memory from heuristics.json
    if mapping['tf_type'] in heuristics_kb:
        for error_key, snippet in heuristics_kb[mapping['tf_type']].items():
            heuristics.warn_legacy_rule_used(mapping['tf_type'], error_key, snippet)
            cmd = snippet.strip().upper()
            if cmd == "OMIT":
                keys_to_omit.append(error_key)
            elif cmd.startswith("IGNORE"):
                # Handles both raw "IGNORE" and legacy "IGNORE:fieldname"
                field = cmd.split(":", 1)[1].strip() if ":" in cmd else error_key
                if field not in fields_to_ignore:
                    fields_to_ignore.append(field)
            else:
                expert_snippets.append(snippet) # It's raw HCL code

    # 2. Handle Automated DB Snippet lookup for the CURRENT error
    # (Only used if no manual snippet is provided)
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
                # Add it to snippets ONLY if it isn't already there from step 1
                if db_snippet not in expert_snippets:
                    expert_snippets.append(db_snippet)

    # 3. Handle Manual Input (Overrides DB)
    if manual_snippet:
        cmd = manual_snippet.strip().upper()
        if cmd == "OMIT":
            if manual_trigger_key not in keys_to_omit: keys_to_omit.append(manual_trigger_key)
        elif cmd.startswith("IGNORE"):
            field = cmd.split(":", 1)[1].strip() if ":" in cmd else manual_trigger_key
            if field not in fields_to_ignore: fields_to_ignore.append(field)
        else:
             if manual_snippet not in expert_snippets:
                 expert_snippets.append(manual_snippet)

    # 3b. PR-7 sanity gate: filter pure-computed names out of ignore_changes.
    # Same gate as in `_generate_and_save_hcl` — applies to the correction
    # path so re-runs with manual snippets can't reintroduce the bug either.
    try:
        from . import schema_oracle as _so
        _oracle = _so.get_oracle()
        if _oracle.has(mapping['tf_type']):
            _pure = set(_oracle.computed_only_paths(mapping['tf_type']))
            _bad = [f for f in fields_to_ignore if f in _pure]
            if _bad:
                print(f"   - ⚠️  Dropping {len(_bad)} pure-computed name(s) from "
                      f"ignore_changes (Terraform rejects these): {_bad}")
                fields_to_ignore = [f for f in fields_to_ignore if f not in _pure]
    except Exception as _e:  # noqa: BLE001 - fail open
        print(f"   - WARN: pure-computed sanity gate skipped ({_e})")

    # 4. JSON Scrubbing
    if keys_to_omit:
        print(f"   - 🛡️  Scrubbing JSON keys for correction: {keys_to_omit}")
        resource_json = scrub_json(resource_json, keys_to_omit)

    # --- THE DEFINITIVE FIX: Formatting instructions for the generator ---
    # We must explicitly build the formatted string the hcl_generator expects
    expert_instructions = []
    
    if fields_to_ignore:
        # Create the specific IGNORE_LIST format
        expert_instructions.append("IGNORE_LIST:" + ",".join(fields_to_ignore))
        
    if expert_snippets:
        # Join all separate code blocks with newlines
        expert_instructions.append("\n".join(expert_snippets))
    
    expert_snippet_str = "\n".join(expert_instructions) if expert_instructions else None
    # -------------------------------------------------------------------

    # PR-10: re-detect resource mode (the snapshot was already pruned upstream,
    # so detection still works — we just need the addendum back for the prompt).
    _correction_mode_addendum = ""
    try:
        _data = json.loads(resource_json)
        _modes = resource_mode.detect_modes(_data, mapping['tf_type'])
        _correction_mode_addendum = resource_mode.mode_prompt_addendum(_modes)
    except (json.JSONDecodeError, TypeError):
        pass

    # 5. Generate HCL
    corrected_hcl = hcl_generator.generate_hcl_from_json(
        resource_json, mapping['tf_type'], mapping['hcl_name'], attempt=attempt_num,
        previous_error=previous_error, schema=schema,
        expert_snippet=expert_snippet_str, # Pass the carefully formatted string
        keys_to_omit=keys_to_omit,
        mode_addendum=_correction_mode_addendum,
    )
    
    if not corrected_hcl: return (mapping, "LLM failed to provide a correction.", False)

    # 6. HCL Scrubbing
    from .run import _scrub_hcl 
    corrected_hcl = _scrub_hcl(corrected_hcl, keys_to_omit)

    with open(mapping["filename"], "w", encoding='utf-8-sig') as f: f.write(corrected_hcl)
    print(f"✅ Saved corrected HCL for '{mapping['resource_name']}'.")
    time.sleep(1)

    print(f"   - Re-attempting import for '{mapping['resource_name']}'...")
    terraform_client.import_resource(mapping, force_refresh=True)

    is_success, plan_output = terraform_client.plan_for_resource(mapping)

    return (mapping, plan_output, is_success)

def run_workflow():
    """Main function with RAG-powered, parallel bulk import and self-correction."""
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

    # Plain `terraform init` (no -upgrade): respects the committed
    # .terraform.lock.hcl so providers resolve to the exact pinned versions.
    # Using -upgrade here would re-resolve to latest-within-constraints on
    # every workflow run and silently mutate the lock file, defeating the
    # reproducibility guarantee that committing the lock provides.
    # `_ensure_initialized()` still uses upgrade=True for the missing-lock
    # case (legitimate first-time setup); only this unconditional pre-import
    # init was problematic.
    terraform_client.init()
    for result in successful_generations: terraform_client.import_resource(result['mapping'])

    print("\n--- Initial Verification of All Generated Files (per-resource scoped plans) ---")
    all_results = []
    for result in successful_generations:
        is_success, output = terraform_client.plan_for_resource(result['mapping'])
        result['is_success'] = is_success
        if not is_success: result['data']['error'] = output
        all_results.append(result)

    successful_imports = [r for r in all_results if r['is_success']]
    failed_imports = [r for r in all_results if not r['is_success']]

    if failed_imports:
        # Path 1: classify failures into self_broken vs blocked-by-sibling
        # so we work on causes first. Blocked items often auto-resolve when
        # the sibling that caused the directory-wide config-load error is
        # fixed — _refresh_blocked_after_fix() handles the promotion.
        _annotate_blockage(failed_imports)
        failed_imports.sort(key=lambda i: 0 if i.get('_blockage') == 'self_broken' else 1)
        n_self = sum(1 for i in failed_imports if i.get('_blockage') == 'self_broken')
        n_blocked = sum(1 for i in failed_imports if i.get('_blockage') == 'blocked')

        print("\n" + "="*70)
        print(f"--- Starting Interactive Correction for {len(failed_imports)} Failed Resources ---")
        if n_blocked:
            print(f"    {n_self} self-broken · {n_blocked} blocked by sibling files")
            print(f"    Blocked items will auto-reverify after their blocker is fixed.")
        print("="*70)

        for failed_item in list(failed_imports):
            # Path 1: skip items that were auto-promoted by a sibling fix
            # since the previous iteration of this loop.
            if failed_item not in failed_imports:
                continue

            mapping = failed_item['mapping']
            current_error = failed_item['data']['error']
            resource_json = failed_item['data']['json']
            ai_attempt_count = 0

            while True:
                clean_error = _clean_terraform_output(current_error)
                # Path 1: surface the BLOCKED indicator so the operator
                # understands they're seeing a sibling's error, not this
                # resource's own error.
                blockage = failed_item.get('_blockage', 'self_broken')
                blocker = failed_item.get('_blocker')
                if blockage == 'blocked' and blocker:
                    print(f"\n⏸️  RESOURCE: '{mapping['resource_name']}' "
                          f"BLOCKED by sibling file: {blocker}")
                    print(f"    The error below originates in {blocker}, NOT in "
                          f"this resource's .tf file.")
                    print(f"    Recommended: Skip ([3]) — this resource will "
                          f"auto-reverify once {blocker} is fixed.")
                    print(f"--- TERRAFORM ERROR (from sibling) ---")
                else:
                    print(f"\n🛑 RESOURCE: '{mapping['resource_name']}'\n"
                          f"--- TERRAFORM DIFF / ERROR ---")
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
                    
                    # 1. Parse the user input cleanly
                    raw_snippet = user_input
                    snippet_to_save = raw_snippet
                    is_surgical_command = False
                    
                    if raw_snippet.upper().startswith("SNIPPET:"):
                        try:
                            lines = raw_snippet.split('\n', 1)
                            command, field_name = lines[0].split(":", 1)
                            field_name = field_name.strip()
                            actual_hcl_code = lines[1].strip() if len(lines) > 1 else ""

                            if field_name and actual_hcl_code:
                                error_trigger_key = field_name
                                snippet_to_save = actual_hcl_code
                                is_surgical_command = True
                                print(f"   - 🛡️  Surgical SNIPPET registered for field: '{field_name}'")
                            else:
                                print("   ❌ Invalid SNIPPET syntax. Must provide a field name AND code on the next lines.")
                                continue
                        except ValueError:
                            print("   ❌ Invalid SNIPPET syntax. Use SNIPPET:fieldname")
                            continue
                            
                    elif raw_snippet.upper().startswith("OMIT:") or raw_snippet.upper().startswith("IGNORE:"):
                        try:
                            command, field_name = raw_snippet.split(":", 1)
                            field_name = field_name.strip()
                            if field_name:
                                error_trigger_key = field_name 
                                # --- THE FIX: Save only the clean command name ---
                                snippet_to_save = "IGNORE" if command.upper().startswith("IGNORE") else "OMIT"
                                # -------------------------------------------------
                                is_surgical_command = True
                                print(f"   - 🛡️  Surgical {snippet_to_save} registered for field: '{field_name}'")
                        except ValueError:
                            print("   ❌ Invalid syntax. Use OMIT:fieldname or IGNORE:fieldname")
                            continue

                    # 2. Save the clean rule to the knowledge base
                    heuristics.save_heuristic(mapping['tf_type'], error_trigger_key, snippet_to_save)
                    print("--- Retrying immediately with newly learned heuristic... ---")
                    
                    # 3. Prepare the arguments for the generator
                    keys_to_omit_immediate = [error_trigger_key] if snippet_to_save == "OMIT" else []
                    
                    # --- THE FIX: Format specifically for the generator here ---
                    snippet_to_pass = None
                    if snippet_to_save == "IGNORE":
                        snippet_to_pass = f"IGNORE_LIST:{error_trigger_key}"
                    elif snippet_to_save != "OMIT":
                        snippet_to_pass = snippet_to_save
                    # -----------------------------------------------------------

                    if keys_to_omit_immediate:
                        print(f"   - 🛡️  Proactively scrubbing key: {keys_to_omit_immediate}")
                        resource_json = scrub_json(resource_json, keys_to_omit_immediate)

                    # PR-10: re-detect mode for the manual-snippet retry path too.
                    _manual_mode_addendum = ""
                    try:
                        _data = json.loads(resource_json)
                        _modes = resource_mode.detect_modes(_data, mapping['tf_type'])
                        _manual_mode_addendum = resource_mode.mode_prompt_addendum(_modes)
                    except (json.JSONDecodeError, TypeError):
                        pass

                    corrected_hcl = hcl_generator.generate_hcl_from_json(
                        resource_json, mapping['tf_type'], mapping['hcl_name'],
                        attempt=2, schema=schemas.get(mapping['tf_type']),
                        expert_snippet=snippet_to_pass, # Pass the formatted instruction
                        keys_to_omit=keys_to_omit_immediate,
                        mode_addendum=_manual_mode_addendum,
                    )

                    if corrected_hcl:
                        from .run import _scrub_hcl 
                        corrected_hcl = _scrub_hcl(corrected_hcl, keys_to_omit_immediate)
                        
                        with open(mapping["filename"], "w", encoding='utf-8-sig') as f: f.write(corrected_hcl)
                        time.sleep(1)
                        
                        print("   - Re-attempting import with corrected HCL...")
                        terraform_client.import_resource(mapping, force_refresh=True)
                        is_success, plan_output = terraform_client.plan_for_resource(mapping)
                        
                        if is_success:
                            print(f"✅ Human-in-the-Loop Correction SUCCEEDED for '{mapping['resource_name']}'!")
                            successful_imports.append(failed_item)
                            failed_imports.remove(failed_item)
                            # Path 1: this fix may have unblocked siblings.
                            _refresh_blocked_after_fix(
                                failed_imports, successful_imports, mapping['filename']
                            )
                            break
                        else:
                            print("❌ Correction failed. Returning to menu.")
                            current_error = plan_output
                            # Path 1: re-classify in case error file changed.
                            failed_item['data']['error'] = plan_output
                            kind, blocker = _classify_blockage(failed_item)
                            failed_item['_blockage'] = kind
                            failed_item['_blocker'] = blocker
                    else:
                         print("❌ LLM failed to generate correction. Returning to menu.")
                    continue 

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
                            # Path 1: this fix may have unblocked siblings.
                            _refresh_blocked_after_fix(
                                failed_imports, successful_imports, mapping['filename']
                            )
                            break
                        else:
                            current_error = output
                            # Path 1: re-classify in case the error file
                            # changed (own-file fixed but sibling now blocks).
                            failed_item['data']['error'] = output
                            kind, blocker = _classify_blockage(failed_item)
                            failed_item['_blockage'] = kind
                            failed_item['_blocker'] = blocker

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
    print("Workflow finished.")

if __name__ == "__main__":
    run_workflow()