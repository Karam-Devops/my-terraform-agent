# agent_nodes.py

import os
import subprocess
import json
import shutil
import time
from langchain_core.messages import HumanMessage, ToolMessage

from .llm_provider import llm
from .config import config
from .agent_state import AgentState

def create_generation_prompt(user_request: str) -> str:
    """Creates the initial instruction prompt for the LLM."""
    return f"""
You are a world-class Google Cloud Infrastructure Architect specializing in writing production-grade Terraform. Your task is to generate a complete and accurate set of Terraform files based on the user's request.

**ABSOLUTE CRITICAL RULES - YOU MUST FOLLOW THESE:**
1.  **NO HALLUCINATED ARGUMENTS:** Your highest priority is to only use arguments that exist in the official HashiCorp Google Provider v5.0 documentation. The `google_secret_manager_secret` resource does NOT have an argument named `automatic`.
2.  **JSON OUTPUT ONLY:** Your entire response MUST be a single, valid JSON object. Do not add any commentary or markdown.
3.  **FILE & DIRECTORY STRUCTURE:**
    - The keys of the JSON object must be the full, relative file paths (e.g., "modules/gcs-bucket/main.tf").
    - The values MUST BE a plain string containing the HCL code. The value must NOT be another JSON object.
    - You MUST use the 'modules/' directory for reusable components.

---
**SPECIAL INSTRUCTIONS FOR GKE SERVICE MESH:**
- If the user requests GKE with Service Mesh, you MUST follow the modern, Fleet-based approach.
- This means the `main.tf` for the GKE module MUST contain these four resources: `google_project_service`, `google_container_cluster` (with `fleet` block), `google_gke_hub_feature`, and `google_gke_hub_feature_membership`.
---

**User Request:** "{user_request}"

Begin generation now.
"""

def generate_code_node(state: AgentState) -> dict:
    """Generates the initial code and validates its internal structure."""
    print("--- 1. GENERATING TERRAFORM CODE ---")
    user_request = state['messages'][0].content
    prompt = create_generation_prompt(user_request)
    response = llm.invoke([HumanMessage(content=prompt)])
    try:
        json_str = response.content
        start_index = json_str.find('{')
        end_index = json_str.rfind('}')
        if start_index == -1 or end_index == -1:
            raise json.JSONDecodeError("Could not find JSON object in LLM response.", json_str, 0)
        cleaned_json_str = json_str[start_index : end_index + 1]
        files_dict = json.loads(cleaned_json_str)

        # --- NEW: Add a structural validation step ---
        # Ensure that every value in the dictionary is a string, not another object.
        for file_path, content in files_dict.items():
            if not isinstance(content, str):
                raise TypeError(f"Invalid content for file '{file_path}': The LLM generated a JSON object instead of a code string.")

        return {"files_to_write": files_dict, "messages": [response], "iteration_count": 0}
    except (json.JSONDecodeError, TypeError) as e:
        error_message = f"LLM response failed structural validation. Error: {e}"
        print(f"❌ FATAL: {error_message}")
        print("Raw LLM Output:\n", response.content)
        return {"files_to_write": {}, "messages": [ToolMessage(content=error_message, tool_call_id="json_parser")], "iteration_count": 0}

def fix_code_node(state: AgentState) -> dict:
    """Takes a validation error and attempts to fix the code."""
    print("--- ATTEMPTING TO FIX CODE ---")
    error_message = state['messages'][-1].content
    current_code_dict = state['files_to_write']
    code_as_string = json.dumps(current_code_dict, indent=2)

    prompt_parts = [
        "You are a Terraform debugging expert. The Terraform configuration you last generated is invalid.",
        "Your task is to fix the error and provide a fully corrected version of the complete file structure as a single JSON object.",
        "\n**THE BROKEN CODE (JSON):**\n```json\n", code_as_string, "\n```\n",
        "\n**THE `terraform validate` ERROR:**\n```\n", error_message, "\n```\n",
        "\n**INSTRUCTION:**\nAnalyze the error and the broken code. Fix the code based on the error message.",
        "Return the complete, corrected code for ALL files as a single JSON object. Ensure all file contents are plain strings. Do not add any commentary."
    ]
    prompt = "\n".join(prompt_parts)
    
    response = llm.invoke([HumanMessage(content=prompt)])
    
    try:
        json_str = response.content
        start_index = json_str.find('{')
        end_index = json_str.rfind('}')
        if start_index == -1 or end_index == -1:
            raise json.JSONDecodeError("Could not find JSON object in LLM fix response.", json_str, 0)
        cleaned_json_str = json_str[start_index : end_index + 1]
        files_dict = json.loads(cleaned_json_str)

        # --- NEW: Also apply structural validation to the fix attempt ---
        for file_path, content in files_dict.items():
            if not isinstance(content, str):
                raise TypeError(f"Invalid content in FIX for file '{file_path}': The LLM generated a JSON object instead of a code string.")

        return {"files_to_write": files_dict, "iteration_count": state['iteration_count'] + 1}
    except (json.JSONDecodeError, TypeError) as e:
        error_message = f"LLM fix response failed structural validation. Error: {e}"
        print(f"❌ FATAL: {error_message}")
        print("Raw LLM Output:\n", response.content)
        return {"final_report": "Agent failed to fix the code and produced invalid structure. Halting."}

def validate_code_node(state: AgentState) -> dict:
    """Validates the Terraform code within each generated module directory."""
    print("--- VALIDATING TERRAFORM CODE (MODULES) ---")
    files_to_write = state.get("files_to_write")
    if not files_to_write:
        return {"messages": [ToolMessage(content="Validation skipped: No files.", tool_call_id="validator")]}
    module_dirs = set(os.path.dirname(p) for p in files_to_write.keys() if p.startswith("modules/"))
    if not module_dirs:
        return {"messages": [ToolMessage(content="Terraform code is valid.", tool_call_id="validator")]}
    
    for module_path in module_dirs:
        temp_dir = f"./temp_validation_{module_path.replace('/', '_')}"
        try:
            os.makedirs(temp_dir, exist_ok=True)
            print(f"--- Validating Module: {module_path} ---")
            for file_path, content in files_to_write.items():
                if os.path.dirname(file_path) == module_path:
                    # The error happens here if 'content' is not a string
                    with open(os.path.join(temp_dir, os.path.basename(file_path)), "w", encoding='utf-8') as f:
                        f.write(content)
            
            from common.terraform_path import resolve_terraform_path
            terraform_path = resolve_terraform_path()
            
            print(f"Running 'terraform init' in {temp_dir}...")
            init_proc = subprocess.run([terraform_path, "init", "-input=false"], cwd=temp_dir, capture_output=True, text=True, check=False)
            if init_proc.returncode != 0:
                error = f"Terraform Init Failed in module '{module_path}':\n{init_proc.stderr}"
                return {"messages": [ToolMessage(content=error, tool_call_id="validator")]}
                
            print("Running 'terraform validate'...")
            validate_proc = subprocess.run([terraform_path, "validate"], cwd=temp_dir, capture_output=True, text=True, check=False)
            if validate_proc.returncode != 0:
                error = f"Terraform Validation Failed in module '{module_path}':\n{validate_proc.stderr}"
                return {"messages": [ToolMessage(content=error, tool_call_id="validator")]}
        
        finally:
        # --- THIS IS THE CRITICAL CHANGE ---
        # Instead of shutil.rmtree(), use the new resilient function.
            if os.path.exists(temp_dir):
                print(f"   Cleaning up temporary directory...")
                force_delete_directory(temp_dir)

    return {"messages": [ToolMessage(content="Terraform code is valid.", tool_call_id="validator")]}

def file_writer_node(state: AgentState) -> dict:
    """Writes the final, validated files to disk."""
    print("--- 3. WRITING FILES TO DISK ---")
    # This node does not need changes, as it will now only receive valid data.
    files_to_write = state.get('files_to_write')
    if not files_to_write:
        report = "File writing skipped: No files to write."
        return {"final_report": report}
    base_dir = config.OUTPUT_DIR
    for file_path, content in files_to_write.items():
        full_path = os.path.join(base_dir, file_path)
        directory = os.path.dirname(full_path)
        try:
            os.makedirs(directory, exist_ok=True)
            with open(full_path, 'w', encoding='utf-8') as f:
                f.write(content)
            print(f"  -> Wrote: {full_path}")
        except Exception as e:
            report = f"FATAL: Error writing file {full_path}: {e}"
            return {"final_report": report}
    iteration_msg = f"after {state.get('iteration_count', 0)} correction(s)" if state.get('iteration_count', 0) > 0 else "without correction"
    report = f"Success! Wrote {len(files_to_write)} files to '{base_dir}' {iteration_msg}."
    return {"final_report": report}

def force_delete_directory(path: str, max_retries: int = 5):
    """
    Robustly deletes a directory, retrying on Windows PermissionError.
    This is necessary to handle race conditions with Terraform file locks.
    """
    def on_rm_error(func, path, exc_info):
        """Error handler for shutil.rmtree."""
        # Check if the error is a PermissionError, typical of file locks
        if issubclass(exc_info[0], PermissionError):
            print(f"   [!] File lock detected on {path}. Retrying...")
        else:
            raise # Re-raise other errors

    for i in range(max_retries):
        try:
            shutil.rmtree(path, onerror=on_rm_error)
            # If we get here, the deletion was successful
            print(f"   [✓] Successfully deleted temporary directory: {path}")
            return
        except PermissionError:
            wait_time = 0.1 * (i + 1) # Wait a bit longer each time
            print(f"   [!] Deletion failed on attempt {i+1}. Waiting {wait_time:.2f}s...")
            time.sleep(wait_time)
        except Exception as e:
            print(f"❌ An unexpected error occurred during directory cleanup: {e}")
            break # Stop on other errors like FileNotFoundError

    print(f"❌ FATAL: Could not delete directory '{path}' after {max_retries} attempts.")