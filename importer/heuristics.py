# my-terraform-agent/importer/heuristics.py

import json
import os
import re

HEURISTICS_FILE = os.path.join(os.path.dirname(__file__), 'heuristics.json')

def load_heuristics():
    """Loads the entire heuristics knowledge base from the JSON file."""
    if not os.path.exists(HEURISTICS_FILE):
        return {}
    try:
        with open(HEURISTICS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (IOError, json.JSONDecodeError):
        return {}

def generate_error_signature(error_message, resource_type):
    """
    Creates a simplified, searchable signature from a complex Terraform error.
    """
    if not error_message:
        return f"{resource_type}:unknown_error"

    # --- THE FIX: Simpler, more robust Regex that ignores newlines ---
    # Look for "Blocks of type "X" are not expected here"
    block_match = re.search(r'Blocks of type "([^"]+)" are not expected here', error_message, re.IGNORECASE)
    if block_match:
        block_name = block_match.group(1)
        print(f"🧠 HEURISTICS: Identified error signature for unsupported block: '{block_name}'")
        return block_name 

    # Look for "An argument named "X" is not expected here"
    arg_match = re.search(r'An argument named "([^"]+)" is not expected here', error_message, re.IGNORECASE)
    if arg_match:
        arg_name = arg_match.group(1)
        print(f"🧠 HEURISTICS: Identified error signature for unsupported argument: '{arg_name}'")
        return arg_name
    # --- END OF FIX ---

    print("🧠 HEURISTICS: Could not identify a specific error pattern. Using generic signature.")
    return "generic_error"

def get_heuristic_for_error(resource_type, error_signature):
    """Finds a relevant heuristic for a given error signature."""
    heuristics = load_heuristics()
    retrieved_solution = heuristics.get(resource_type, {}).get(error_signature)
    
    if retrieved_solution:
        print(f"🧠 HEURISTICS: Found a past solution for '{error_signature}'.")
        return retrieved_solution
    return None

def save_heuristic(resource_type, error_signature, correct_hcl_snippet):
    """Saves a new, human-verified heuristic to the knowledge base."""
    if not error_signature or error_signature == "generic_error":
        print("🧠 HEURISTICS: Not saving solution for a generic or unknown error pattern.")
        return

    print(f"🧠 HEURISTICS: Learning a new rule for '{resource_type}' triggered by '{error_signature}'...")
    heuristics = load_heuristics()
    
    if resource_type not in heuristics:
        heuristics[resource_type] = {}
        
    heuristics[resource_type][error_signature] = correct_hcl_snippet
    
    try:
        with open(HEURISTICS_FILE, "w", encoding="utf-8") as f:
            json.dump(heuristics, f, indent=2)
        print("   - ✅ Knowledge base updated successfully.")
    except IOError as e:
        print(f"   - ❌ Failed to save heuristic: {e}")