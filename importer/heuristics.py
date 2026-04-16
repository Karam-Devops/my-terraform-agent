# my-terraform-agent/importer/heuristics.py

import json
import os
import re

HEURISTICS_FILE = os.path.join(os.path.dirname(__file__), 'heuristics.json')

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
    return heuristics.get(resource_type, {}).get(error_signature)

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