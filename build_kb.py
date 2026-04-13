# my-terraform-agent/build_kb.py

import requests
import json
import os
import re

# Import the new mapping
from importer.config import ASSET_TO_TERRAFORM_MAP, TF_TYPE_TO_GITHUB_DOC_PATH

BASE_URL = "https://raw.githubusercontent.com/hashicorp/terraform-provider-google/main/website/docs/r/"
FILE_SUFFIX = ".html.markdown"
KB_DIR = "importer/knowledge_base"

def get_docs_from_github(resource_type):
    """Fetches and parses documentation using the definitive path mapping from config."""
    print(f"Fetching documentation for: {resource_type} from GitHub...")

    # --- THIS IS THE FIX ---
    # Look up the correct URL path from our new config dictionary.
    doc_path_component = TF_TYPE_TO_GITHUB_DOC_PATH.get(resource_type)
    
    if not doc_path_component:
        print(f"  ❌ No documentation path configured for '{resource_type}' in config.py. Skipping.")
        return None
    # --- END OF FIX ---

    url = f"{BASE_URL}{doc_path_component}{FILE_SUFFIX}"
    
    try:
        response = requests.get(url, timeout=15)
        response.raise_for_status()
        markdown_content = response.text
        
        arguments = []
        try:
            arg_section_split = re.split(r'##\s*Argument Reference', markdown_content, flags=re.IGNORECASE)
            if len(arg_section_split) < 2: raise ValueError("Argument Reference section not found")
            
            main_arg_block = arg_section_split[1].split('\n## ')[0]
            arg_pattern = re.compile(r"^\s*[\*\-]\s+`([^`]+)`", re.MULTILINE)
            found_args = arg_pattern.findall(main_arg_block)
            
            for arg_name in found_args:
                line_finder_pattern = re.compile(f".*`{re.escape(arg_name)}`.*", re.MULTILINE)
                line_match = line_finder_pattern.search(main_arg_block)
                line_text = line_match.group(0) if line_match else ""
                arguments.append({"name": arg_name, "required": "(Required)" in line_text})
        
        except (ValueError, IndexError) as e:
            print(f"  ⚠️  Could not parse arguments for {resource_type}. Error: {e}")
            return {"resource_type": resource_type, "arguments": []}

        print(f"  ✅ Parsed {len(arguments)} arguments for {resource_type}.")
        return {"resource_type": resource_type, "arguments": arguments}

    except requests.RequestException as e:
        print(f"  ❌ Failed to fetch from GitHub API. URL: '{url}'. Error: {e}")
        return None
    except Exception as e:
        print(f"  ❌ An unexpected error occurred for {resource_type}. Error: {e}")
        return None

def build_knowledge_base():
    """Main function to build the entire knowledge base."""
    print("--- Starting Knowledge Base Build Process (Definitive Path Mapping) ---")
    if not os.path.exists(KB_DIR):
        os.makedirs(KB_DIR)
        
    resource_types = sorted(list(set(ASSET_TO_TERRAFORM_MAP.values())))
    
    for rt in resource_types:
        doc_data = get_docs_from_github(rt)
        if doc_data:
            file_path = os.path.join(KB_DIR, f"{rt}.json")
            with open(file_path, "w", encoding="utf-8") as f:
                json.dump(doc_data, f, indent=2)
            print(f"  -> Saved knowledge to {file_path}\n")
            
    print("--- Knowledge Base Build Process Finished ---")

if __name__ == "__main__":
    build_knowledge_base()