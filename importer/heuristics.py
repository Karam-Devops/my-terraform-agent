# my-terraform-agent/importer/heuristics.py

import json
import os
import re

HEURISTICS_FILE = os.path.join(os.path.dirname(__file__), 'heuristics.json')


# ---------------------------------------------------------------------------
# Deprecation tracking (PR-6)
# ---------------------------------------------------------------------------
#
# heuristics.json is the legacy "remember what the LLM got wrong" memory.
# Most of the rules it has accumulated address one of two failure classes:
#
#   1. Pure-computed fields the LLM emitted    -> now handled by PR-3
#                                                  (snapshot_scrubber)
#   2. Optional+computed perpetual diffs       -> now handled by PR-4
#                                                  (lifecycle_planner)
#   3. Unknown-block / schema-shape errors     -> now handled by PR-5
#                                                  (schema_prompt summary)
#   4. Service-managed labels                  -> now handled by PR-6
#                                                  (filter_auto_labels)
#
# We're not deleting the file yet — there's no telemetry on which rules
# still earn their keep. Instead, every time a rule fires we print a
# loud one-line marker so operators (and us, in logs) can see how often
# the legacy path is engaged. When that count hits zero across a release,
# the whole subsystem can be retired.
#
# The set is per-process; no cross-run state.

_warned_rules: set = set()


def warn_legacy_rule_used(tf_type: str, error_key: str, snippet) -> None:
    """Emit a one-line deprecation marker the first time a given
    (tf_type, error_key) heuristic is consulted in this process."""
    fp = f"{tf_type}::{error_key}"
    if fp in _warned_rules:
        return
    _warned_rules.add(fp)
    if isinstance(snippet, str):
        kind = snippet.strip().upper().split(":", 1)[0] or "SNIPPET"
        if kind not in ("OMIT", "IGNORE"):
            kind = "SNIPPET"
    else:
        kind = "SNIPPET"
    print(
        f"   - ⚠️  [DEPRECATED] heuristics.json rule fired: "
        f"{tf_type} / {error_key} ({kind}). "
        f"Schema-oracle pipeline (PR-3..PR-6) should cover this; "
        f"if it does not, file an issue so we can fix the oracle path "
        f"instead of patching heuristics."
    )

def load_heuristics():
    """Loads the heuristics. Fails loudly if the JSON is manually corrupted."""
    if not os.path.exists(HEURISTICS_FILE):
        return {}
    try:
        with open(HEURISTICS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except json.JSONDecodeError as e:
        # --- THE FIX: Never silently overwrite a corrupted file ---
        print(f"\n❌ CRITICAL ERROR: Your heuristics.json file is corrupted or formatted incorrectly.")
        print(f"   Details: {e}")
        print("   Please fix the JSON syntax before running the agent to prevent data loss.")
        # We return None to signal a hard failure, preventing save_heuristic from overwriting it
        return None 
    except IOError:
        return {}

def generate_error_signature(error_message, resource_type):
    if not error_message: return f"{resource_type}:unknown_error"

    block_match = re.search(r'Blocks of type "([^"]+)" are not expected here', error_message, re.IGNORECASE)
    if block_match: return block_match.group(1) 

    arg_match = re.search(r'An argument named "([^"]+)" is not expected here', error_message, re.IGNORECASE)
    if arg_match: return arg_match.group(1)

    return "generic_error"

def get_heuristic_for_error(resource_type, error_signature):
    heuristics = load_heuristics()
    if heuristics is None: return None # Safety check
    snippet = heuristics.get(resource_type, {}).get(error_signature)
    if snippet is not None:
        warn_legacy_rule_used(resource_type, error_signature, snippet)
    return snippet

def save_heuristic(resource_type, error_signature, correct_snippet):
    """Saves a rule safely, refusing to run if the file is corrupted."""
    if isinstance(correct_snippet, str):
        is_omit_rule = correct_snippet.strip().upper() == "OMIT"
    else:
        is_omit_rule = False

    if not error_signature or (error_signature == "generic_error" and not is_omit_rule):
        print("🧠 HEURISTICS: Not saving solution for a generic or unknown error pattern.")
        return

    heuristics = load_heuristics()
    
    # --- THE FIX: Abort save if the file is corrupted ---
    if heuristics is None:
        print("   - ❌ Cannot save new heuristic because heuristics.json is currently corrupted.")
        return
    # ----------------------------------------------------

    print(f"🧠 HEURISTICS: Learning a new rule for '{resource_type}' triggered by '{error_signature}'...")
    
    if resource_type not in heuristics:
        heuristics[resource_type] = {}
        
    heuristics[resource_type][error_signature] = correct_snippet
    
    try:
        with open(HEURISTICS_FILE, "w", encoding="utf-8") as f:
            json.dump(heuristics, f, indent=2)
        print("   - ✅ Knowledge base updated successfully.")
    except IOError as e:
        print(f"   - ❌ Failed to save heuristic: {e}")