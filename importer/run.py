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
# Root-level app config — distinct from `importer/config.py`. The root holds
# the project-ID concepts (HOST/TARGET/DEMO) and the resolver that enforces
# the demo-lock safety gate. Aliased to `app_config` to avoid shadowing the
# importer-local `config` module imported above.
from .. import config as app_config
from common.workdir import resolve_project_workdir
from common.errors import EngineError, PreflightError
from common.logging import get_logger
from .results import WorkflowResult

# Module-level logger used by the A+D boundary (workflow_complete event
# emits the full WorkflowResult via result.as_fields()). Other print()
# sites in this file remain untouched in C3 -- they're part of the
# interactive CLI path and get cleaned up in C4 (WARN cluster).
_log = get_logger(__name__)

# NOTE: we DELIBERATELY no longer chdir to the repo root here. Each workflow
# invocation now resolves a per-project working directory via
# common.workdir.resolve_project_workdir(project_id) AFTER the operator has
# entered the project ID. All .tf files, terraform.tfstate, and the
# .terraform/ plugin dir for that project live under that workdir; nothing
# is written to the repo root any more.
#
# History: a previous version of this file did `os.chdir(project_root)` at
# import time, which (a) caused .tf files from different GCP projects to
# commingle in the repo root and (b) meant a single shared terraform.tfstate
# at repo root mixed state across projects -- catastrophic when two demo
# projects were imported in the same shell. Removed as part of the
# per-project workdir refactor (see scripts/migrate_workdir.py for the
# one-shot data migration).

# IGNORE_LIST cumulative state (TODO #8) ----------------------------------
# Per-run cumulative ignore set keyed by .tf filename. Once a field is added
# to a resource's `lifecycle.ignore_changes` from ANY source (heuristic,
# schema-derived auto-ignore, manual operator teach, error-DB lookup), it
# stays in the set for every subsequent regen cycle of that file within
# the same workflow run.
#
# Why this exists: before this layer, fields_to_ignore was rebuilt from
# scratch in three different call sites and each one consulted a different
# subset of sources. Most importantly, lifecycle_planner.derive_* was only
# called in the initial generation path — correction cycles silently
# dropped its contributions and previously-suppressed perpetual diffs
# re-surfaced mid-run. A monotonically-growing per-file set fixes that
# class of bug without each call site having to remember every source.
#
# Cleared at the top of run_workflow() so consecutive workflow invocations
# in the same Python process (relevant once the importer is driven from
# Streamlit) don't carry stale state across user sessions.
_CUMULATIVE_IGNORES_PER_FILE: dict = {}


def _compute_ignore_set(
    mapping,
    resource_json,
    heuristics_kb,
    *,
    current_error=None,
    manual_snippet=None,
    manual_trigger_key=None,
):
    """Build the UNION lifecycle.ignore_changes field set for `mapping`.

    Single source of truth for assembling fields_to_ignore. Replaces three
    separate, drift-prone implementations that each consulted a different
    subset of sources.

    Sources unioned (order is irrelevant — output is sorted):
      1. Cumulative state for this filename from all prior cycles.
      2. Persistent heuristics.json IGNORE entries for this tf_type.
      3. lifecycle_planner.derive_lifecycle_ignores from cloud snapshot.
      4. DB lookup against the current Terraform error signature
         (correction path only).
      5. Manual operator snippet, if it's an IGNORE command.

    A final PR-7 sanity gate strips pure-computed names — Terraform errors
    on `ignore_changes = [terraform_labels]` because there's no configured
    value to compare with.

    Side effect: mutates _CUMULATIVE_IGNORES_PER_FILE[mapping['filename']]
    so the next cycle starts from the union, not from scratch.
    """
    file_key = mapping['filename']
    cumulative = _CUMULATIVE_IGNORES_PER_FILE.setdefault(file_key, set())
    before = set(cumulative)

    # 1. Persistent heuristics IGNORE entries for this tf_type.
    for error_key, snippet in heuristics_kb.get(mapping['tf_type'], {}).items():
        cmd = snippet.strip().upper()
        if cmd.startswith("IGNORE"):
            field = cmd.split(":", 1)[1].strip() if ":" in cmd else error_key
            if field:
                cumulative.add(field)

    # 2. Schema-derived auto-ignores from the (possibly scrubbed) cloud snapshot.
    try:
        cloud_data = json.loads(resource_json) if isinstance(resource_json, str) else (resource_json or {})
    except (json.JSONDecodeError, TypeError):
        cloud_data = {}
    for f in lifecycle_planner.derive_lifecycle_ignores(cloud_data, mapping['tf_type']):
        cumulative.add(f)

    # 3. DB lookup keyed by the current Terraform error.
    if current_error:
        sig = manual_trigger_key or heuristics.generate_error_signature(
            current_error, mapping['tf_type']
        )
        db_snippet = heuristics.get_heuristic_for_error(mapping['tf_type'], sig)
        if db_snippet:
            cmd = db_snippet.strip().upper()
            if cmd.startswith("IGNORE"):
                field = cmd.split(":", 1)[1].strip() if ":" in cmd else sig
                if field:
                    cumulative.add(field)

    # 4. Manual operator snippet from interactive teach.
    if manual_snippet:
        cmd = manual_snippet.strip().upper()
        if cmd.startswith("IGNORE"):
            field = cmd.split(":", 1)[1].strip() if ":" in cmd else manual_trigger_key
            if field:
                cumulative.add(field)

    # 5. PR-7 sanity gate (must run last so it sees the full union).
    try:
        from . import schema_oracle as _so
        oracle = _so.get_oracle()
        if oracle.has(mapping['tf_type']):
            pure = set(oracle.computed_only_paths(mapping['tf_type']))
            bad = cumulative & pure
            if bad:
                print(f"   - [IGNORE-UNION] Dropping {len(bad)} pure-computed "
                      f"name(s) from ignore_changes (Terraform rejects "
                      f"these): {sorted(bad)}")
                cumulative -= pure
    except Exception as e:  # noqa: BLE001 - fail open
        print(f"   - [IGNORE-UNION] WARN: pure-computed sanity gate "
              f"skipped ({e})")

    # Surface only NEW entries so cycle-N logs aren't swamped by the
    # full list every regen. ASCII tag (no emoji) — Windows cp1252 console
    # crashes on emoji output and importer is run on operator machines.
    new_fields = cumulative - before
    if new_fields:
        print(f"   - [IGNORE-UNION] '{mapping['resource_name']}': "
              f"+{sorted(new_fields)} (total={len(cumulative)})")

    return sorted(cumulative)


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

def _map_asset_to_terraform(selected_asset, project_id, workdir):
    """Build the per-resource mapping dict consumed by the rest of the importer.

    `workdir` is the absolute path returned by resolve_project_workdir() for
    THIS project. It's stuck onto the mapping so every downstream consumer
    (file writes here in run.py; subprocess calls in terraform_client.py)
    can pull it back without needing an extra positional arg through every
    function. `mapping['filename']` stays as a bare basename — joining with
    `mapping['workdir']` is a single os.path.join at the call sites.
    """
    asset_type = selected_asset['assetType']
    tf_type = config.ASSET_TO_TERRAFORM_MAP.get(asset_type)
    if not tf_type: return None

    # ---------------- Identity resolution ----------------
    # For most resources, displayName == name == identifier == HCL-safe label.
    # Service accounts break that assumption: displayName is a human label
    # ("POC Smoke SA"), the gcloud-recognised identifier is the email
    # (poc-sa@<project>.iam.gserviceaccount.com), and neither is a valid
    # HCL identifier (spaces / @ / .). So we separate two concepts:
    #
    #   resource_name  -> what gcloud describe / terraform import need to find
    #                     this resource. For SAs this is the FULL EMAIL.
    #   hcl_name_base  -> a stable, HCL-identifier-safe label used to build
    #                     the resource block label and the .tf filename.
    #                     For SAs we use the LOCAL PART of the email
    #                     (everything before '@') -- always valid HCL.
    #
    # For all other types both collapse to the same value (back-compat).
    if asset_type == 'iam.googleapis.com/ServiceAccount':
        # Asset-search returns the email under additionalAttributes.email,
        # NOT under displayName. Fallback to the last segment of the asset
        # name (which is also the email in GCP's URN scheme) if that key
        # is ever missing.
        sa_email = (
            selected_asset.get('additionalAttributes', {}).get('email')
            or selected_asset['name'].split('/')[-1]
        )
        resource_name = sa_email
        hcl_name_base = sa_email.split('@', 1)[0]  # local part, always HCL-safe
    else:
        resource_name = selected_asset.get('displayName') or selected_asset['name'].split('/')[-1]
        hcl_name_base = resource_name

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
        if tf_type == 'google_service_account':
            # Use the ACTUAL email (already resolved above into resource_name),
            # not displayName. The previous code read displayName and produced
            # import_ids like 'project/POC Smoke SA' that terraform import
            # could never resolve.
            format_vars['email'] = resource_name
        if tf_type == 'google_container_node_pool' and 'clusters' in parts:
             format_vars['cluster'] = parts[parts.index('clusters') + 1]
        import_id = import_id_format.format(**format_vars)

    return {
        "tf_type": tf_type, "hcl_name": hcl_name_base.replace('-', '_'),
        "resource_name": resource_name, "import_id": import_id,
        "filename": f"{tf_type}_{hcl_name_base.replace('-', '_')}.tf",
        "location": selected_asset.get('location'), "project_id": project_id,
        # Per-project workdir (absolute path). Carried on the mapping so
        # downstream file writes and terraform_client subprocess calls can
        # pull it back without extra plumbing through every signature.
        "workdir": workdir,
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
    expert_snippets = []

    # --- 1. LOAD ALL MEMORY ---
    # IGNORE entries are deferred to _compute_ignore_set() below so they
    # join the cumulative UNION; here we only handle OMIT keys (pre-LLM
    # JSON scrubbing) and raw HCL snippet entries (verbatim injection),
    # which have semantics independent of the ignore_changes set.
    if mapping['tf_type'] in heuristics_kb:
        for error_key, snippet in heuristics_kb[mapping['tf_type']].items():
            heuristics.warn_legacy_rule_used(mapping['tf_type'], error_key, snippet)
            cmd = snippet.strip().upper()
            if cmd == "OMIT":
                keys_to_omit.append(error_key)
            elif cmd.startswith("IGNORE"):
                continue  # routed through _compute_ignore_set
            else:
                expert_snippets.append(snippet) # It's a code block

    # --- 1b. Cumulative IGNORE_LIST (TODO #8) ---
    # One call assembles the union of heuristics + schema-derived
    # auto-ignores + sanity-gated pure-computed denylist, persists the
    # cumulative state for this filename, and returns the sorted list.
    fields_to_ignore = _compute_ignore_set(mapping, resource_json, heuristics_kb)

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
        # Per-project workdir refactor: write into mapping['workdir'] (absolute
        # path resolved upstream by resolve_project_workdir(project_id)) so
        # files for project-A can never overwrite files for project-B in the
        # repo root. mapping['filename'] stays as a basename.
        out_path = os.path.join(mapping["workdir"], mapping["filename"])
        with open(out_path, "w", encoding='utf-8-sig') as f: f.write(generated_hcl)
        print(f"✅ HCL file saved for '{mapping['resource_name']}' -> {out_path}")
        time.sleep(1)
    except IOError as e:
        return (mapping, False, {"error": f"File write error: {e}"})
    
    return (mapping, True, {"hcl": generated_hcl, "json": resource_json})

def _attempt_correction(mapping, resource_json, previous_error, attempt_num, schema, heuristics_kb, manual_snippet=None, manual_trigger_key=None):
    """A unit of work for correction attempts (Automated or Interactive)."""
    keys_to_omit = []
    expert_snippets = []

    # 1. Load global memory from heuristics.json — OMIT keys and raw HCL
    # snippets only. IGNORE entries are routed through _compute_ignore_set
    # below so they join the cumulative UNION (TODO #8).
    if mapping['tf_type'] in heuristics_kb:
        for error_key, snippet in heuristics_kb[mapping['tf_type']].items():
            heuristics.warn_legacy_rule_used(mapping['tf_type'], error_key, snippet)
            cmd = snippet.strip().upper()
            if cmd == "OMIT":
                keys_to_omit.append(error_key)
            elif cmd.startswith("IGNORE"):
                continue  # routed through _compute_ignore_set
            else:
                expert_snippets.append(snippet) # It's raw HCL code

    # 2. Handle Automated DB Snippet lookup for the CURRENT error
    # (Only used if no manual snippet is provided). IGNORE entries here
    # are also routed through _compute_ignore_set.
    error_signature = manual_trigger_key or heuristics.generate_error_signature(previous_error, mapping['tf_type'])

    if not manual_snippet:
        db_snippet = heuristics.get_heuristic_for_error(mapping['tf_type'], error_signature)
        if db_snippet:
            cmd = db_snippet.strip().upper()
            if cmd == "OMIT":
                if error_signature not in keys_to_omit: keys_to_omit.append(error_signature)
            elif cmd.startswith("IGNORE"):
                pass  # routed through _compute_ignore_set
            else:
                # Add it to snippets ONLY if it isn't already there from step 1
                if db_snippet not in expert_snippets:
                    expert_snippets.append(db_snippet)

    # 3. Handle Manual Input (Overrides DB). IGNORE entries here also
    # routed through _compute_ignore_set so they join the union.
    if manual_snippet:
        cmd = manual_snippet.strip().upper()
        if cmd == "OMIT":
            if manual_trigger_key not in keys_to_omit: keys_to_omit.append(manual_trigger_key)
        elif cmd.startswith("IGNORE"):
            pass  # routed through _compute_ignore_set
        else:
             if manual_snippet not in expert_snippets:
                 expert_snippets.append(manual_snippet)

    # 3b. Cumulative IGNORE_LIST (TODO #8) — single call assembles the
    # union of every IGNORE source (heuristics + lifecycle planner +
    # current-error DB lookup + manual snippet) and applies the PR-7
    # pure-computed sanity gate. Sticky across regen cycles for this
    # filename, which is the whole point: previously-suppressed perpetual
    # diffs no longer re-surface mid-run when correction cycles consult
    # a different subset of sources.
    fields_to_ignore = _compute_ignore_set(
        mapping, resource_json, heuristics_kb,
        current_error=previous_error,
        manual_snippet=manual_snippet,
        manual_trigger_key=manual_trigger_key,
    )

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

    # Per-project workdir refactor (see _generate_and_save_hcl for context).
    out_path = os.path.join(mapping["workdir"], mapping["filename"])
    with open(out_path, "w", encoding='utf-8-sig') as f: f.write(corrected_hcl)
    print(f"✅ Saved corrected HCL for '{mapping['resource_name']}'.")
    time.sleep(1)

    print(f"   - Re-attempting import for '{mapping['resource_name']}'...")
    terraform_client.import_resource(mapping, force_refresh=True)

    is_success, plan_output = terraform_client.plan_for_resource(mapping)

    return (mapping, plan_output, is_success)

def run_workflow() -> WorkflowResult:
    """Main function with RAG-powered, parallel bulk import and self-correction.

    A+D return contract (CC-4):
        * RAISES ``PreflightError`` (``common.errors``) when the workflow
          cannot START -- invalid project ID, unresolvable workdir,
          terraform init failure. The ``__main__`` guard and the future
          Streamlit handler catch ``EngineError`` and render
          ``.user_hint``.
        * RETURNS a ``WorkflowResult`` (``importer.results``) when the
          workflow COMPLETES, regardless of per-resource outcomes. The
          result's ``.failed`` and ``.imported`` counts drive the UI
          summary; ``.exit_code`` is used by the CLI entrypoint.

    A zero-selection workflow (no resources discovered, or operator
    cancelled the selection menu) is NOT an error -- it returns a
    zeroed result with ``exit_code == 0``. Orchestrators that alert
    on non-zero exits therefore won't fire for empty projects.
    """
    started = time.monotonic()
    print("🚀 Starting Google Cloud to Terraform Import Workflow...")

    # TODO #8: clear cumulative IGNORE state from any previous workflow
    # invocation in the same Python process. Matters for the Streamlit
    # path where one process serves many user sessions back-to-back.
    _CUMULATIVE_IGNORES_PER_FILE.clear()

    # NOTE: terraform init is DEFERRED until after we know the project_id
    # and have resolved the per-project workdir. The previous code init'd
    # against the repo root, which is exactly the commingling we just got
    # rid of. See common/workdir.py for the rationale.

    # TODO #11: route project-ID input through the central resolver so the
    # DEMO_PROJECT_ID safety lock fires on a fat-finger scan, and so empty
    # input falls back to the TARGET_PROJECT_ID env var (the eventual UI
    # path will pre-fill from env without re-prompting).
    default_hint = (
        f" [{app_config.config.TARGET_PROJECT_ID}]"
        if app_config.config.TARGET_PROJECT_ID else ""
    )
    raw = input(f"Enter your Google Cloud Project ID{default_hint}: ")
    try:
        project_id = app_config.resolve_target_project_id(raw)
    except ValueError as e:
        # A+D: invalid project ID is a preflight failure. Raise so the
        # CLI / Streamlit handler can render .user_hint; caller doesn't
        # need to distinguish "no project entered" from "workflow ran
        # but imported zero" -- those are different shapes.
        raise PreflightError(
            f"project ID validation failed: {e}",
            stage="validate_project_id",
            reason=str(e),
        ) from e
    os.environ["GOOGLE_PROJECT"] = project_id
    print(f"   - Scanning project: {project_id}")
    if app_config.config.DEMO_PROJECT_ID:
        print(f"   - [DEMO-LOCK] Safety gate active: only "
              f"{app_config.config.DEMO_PROJECT_ID!r} is permitted in this env.")

    # Per-project workdir resolution. ValueError on a malformed project_id
    # is path-traversal protection (see common/workdir.py docstring) -- we
    # surface it cleanly rather than letting it bubble.
    try:
        workdir = resolve_project_workdir(project_id)
    except ValueError as e:
        raise PreflightError(
            f"cannot resolve workdir: {e}",
            stage="resolve_workdir",
            reason=str(e),
        ) from e
    print(f"   - 📁 Per-project workdir: {workdir}")
    if not os.path.isdir(os.path.join(workdir, ".terraform")):
        if terraform_client.init(workdir=workdir) is None:
            # terraform init returns None on failure (current contract).
            # Without a usable plugin cache nothing downstream will work,
            # so this is a preflight failure -- raise, don't return.
            raise PreflightError(
                "terraform init failed; cannot proceed without plugin cache",
                stage="terraform_init",
                reason="terraform init returned None (see prior log events)",
            )

    print("\n--- Stage 1: Discovering All Supported Resources in Parallel ---")
    all_discovered_resources = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=config.MAX_DISCOVERY_WORKERS) as executor:
        future_to_asset_type = {executor.submit(gcp_client.discover_resources_of_type, project_id, at): at for at in config.ASSET_TO_TERRAFORM_MAP}
        for future in concurrent.futures.as_completed(future_to_asset_type):
            try:
                resources = future.result()
                if resources: all_discovered_resources.extend(resources)
            except Exception as exc: print(f"❌ Error during discovery: {exc}")

    if not all_discovered_resources:
        # Workflow COMPLETED -- project just has no supported resources.
        # Return a zeroed result (exit 0) rather than raising; orchestrators
        # shouldn't alert on "empty project".
        return _build_empty_result(
            project_id=project_id, selected=0, started=started,
        )
    all_discovered_resources.sort(key=lambda r: r.get('displayName', r.get('name')))

    selected_assets = _present_selection_menu(all_discovered_resources)
    if not selected_assets:
        # Operator cancelled the selection menu -- workflow completed,
        # nothing imported. Zeroed result, exit 0.
        return _build_empty_result(
            project_id=project_id, selected=0, started=started,
        )

    print("\n--- Stage 3: Mapping All Selected Assets ---")
    mappings = [m for m in [_map_asset_to_terraform(asset, project_id, workdir) for asset in selected_assets] if m is not None]
    if not mappings:
        # Every selected asset had an asset_type with no Terraform
        # mapping -- they all fall into the "skipped" bucket. Workflow
        # completed, no failures to flag. Exit 0.
        return _build_empty_result(
            project_id=project_id,
            selected=len(selected_assets),
            started=started,
        )

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
        # All HCL generations failed -- workflow completed, every mapped
        # resource ends up in the failed bucket. exit_code will be 1.
        return _build_result(
            project_id=project_id,
            selected=len(selected_assets),
            imported=0,
            failed=len(mappings),
            started=started,
        )

    # Plain `terraform init` (no -upgrade): respects the committed
    # .terraform.lock.hcl so providers resolve to the exact pinned versions.
    # Using -upgrade here would re-resolve to latest-within-constraints on
    # every workflow run and silently mutate the lock file, defeating the
    # reproducibility guarantee that committing the lock provides.
    # `_ensure_initialized()` still uses upgrade=True for the missing-lock
    # case (legitimate first-time setup); only this unconditional pre-import
    # init was problematic.
    terraform_client.init(workdir=workdir)
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

                    # Format expert_snippet for the generator. For IGNORE we
                    # must pass the FULL cumulative union (TODO #8) — passing
                    # only `error_trigger_key` would silently drop every other
                    # field that earlier cycles or the schema planner already
                    # added to ignore_changes, re-surfacing perpetual diffs
                    # the operator thought were resolved.
                    snippet_to_pass = None
                    if snippet_to_save == "IGNORE":
                        union_fields = _compute_ignore_set(
                            mapping, resource_json, heuristics.load_heuristics(),
                            manual_snippet="IGNORE",
                            manual_trigger_key=error_trigger_key,
                        )
                        if union_fields:
                            snippet_to_pass = f"IGNORE_LIST:{','.join(union_fields)}"
                    elif snippet_to_save != "OMIT":
                        snippet_to_pass = snippet_to_save

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

                        # Per-project workdir refactor (see _generate_and_save_hcl).
                        out_path = os.path.join(mapping["workdir"], mapping["filename"])
                        with open(out_path, "w", encoding='utf-8-sig') as f: f.write(corrected_hcl)
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

    return _build_result(
        project_id=project_id,
        selected=len(selected_assets),
        imported=len(successful_imports),
        failed=len(failed_imports),
        started=started,
    )


def _build_empty_result(*, project_id: str, selected: int,
                        started: float) -> WorkflowResult:
    """Shortcut for zero-activity completions (no discovery / user cancel / no mappings).

    ``selected`` is passed explicitly because the meaning differs by
    call site: 0 for "nothing discovered" / "user cancelled", and
    ``len(selected_assets)`` for "nothing mapped" (everything fell
    into the skipped bucket).
    """
    return _build_result(
        project_id=project_id,
        selected=selected,
        imported=0,
        failed=0,
        started=started,
    )


def _build_result(*, project_id: str, selected: int, imported: int,
                  failed: int, started: float) -> WorkflowResult:
    """Assemble the final WorkflowResult and emit the completion event.

    Centralises three concerns that must stay in lockstep:
        1. Math: skipped = selected - imported - failed (the catch-all
           bucket; see WorkflowResult docstring for why this is correct).
        2. Duration: measured with time.monotonic() so clock adjustments
           don't skew it.
        3. Log emission: one ``workflow_complete`` event per invocation,
           carrying the full result payload via ``result.as_fields()``.
           Dashboards filter on these keys -- they are pinned by the
           WorkflowResult tests.
    """
    skipped = max(0, selected - imported - failed)
    duration_s = round(time.monotonic() - started, 2)
    result = WorkflowResult(
        project_id=project_id,
        selected=selected,
        imported=imported,
        failed=failed,
        skipped=skipped,
        duration_s=duration_s,
    )
    _log.info("workflow_complete", **result.as_fields())
    return result


if __name__ == "__main__":
    # A+D boundary (CC-4):
    #   * PreflightError / any EngineError  -> preflight failure,
    #     exit 2. Surfaces the typed .user_hint to the operator so
    #     they see "The workflow could not start ..." instead of a
    #     stack trace.
    #   * WorkflowResult returned           -> workflow completed;
    #     exit code derived from result.exit_code (0 iff failed == 0).
    #   * Any other unhandled exception     -> bug; exit 3 so CI
    #     can distinguish it from a clean preflight fail.
    try:
        _result = run_workflow()
    except EngineError as _e:
        # Engineer-facing detail already in structured logs; operator
        # sees the UI-safe hint.
        print(f"\n[FAIL] {_e.user_hint}", file=sys.stderr)
        _log.error("workflow_preflight_failed",
                   error_type=type(_e).__name__,
                   **_e.fields)
        sys.exit(2)
    except KeyboardInterrupt:
        print("\n[ABORTED] Workflow cancelled by operator.", file=sys.stderr)
        sys.exit(130)  # Unix convention for SIGINT
    sys.exit(_result.exit_code)