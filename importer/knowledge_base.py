# importer/knowledge_base.py

import json
import os

KB_DIR = os.path.join(os.path.dirname(__file__), 'knowledge_base')

def get_schema_for_resource(resource_type):
    """
    Loads the pre-scraped documentation (schema) for a given resource type.
    """
    file_path = os.path.join(KB_DIR, f"{resource_type}.json")
    print(f"🧠 KNOWLEDGE BASE: Loading schema from {file_path}")
    
    if not os.path.exists(file_path):
        print("   - ❌ Schema file not found. Proceeding without documentation context.")
        return None
        
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            schema_data = json.load(f)
            print("   - ✅ Successfully loaded schema.")
            return schema_data
    except (IOError, json.JSONDecodeError) as e:
        print(f"   - ❌ Error reading schema file: {e}")
        return None