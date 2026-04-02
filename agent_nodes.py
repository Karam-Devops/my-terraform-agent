# agent_nodes.py

import os
import subprocess
import json
import shutil
from langchain_core.messages import HumanMessage, ToolMessage

from llm_provider import llm
from config import config
from agent_state import AgentState

def create_generation_prompt(user_request: str) -> str:
    """Creates the initial instruction prompt for the LLM."""
    return f"""
You are an expert Google Cloud Infrastructure Architect specializing in writing enterprise-grade Terraform. Your task is to generate a complete and accurate set of Terraform files based on the user's request.

**ABSOLUTE CRITICAL RULES - YOU MUST FOLLOW THESE:**
1.  **NO HALLUCINATED ARGUMENTS:** Your highest priority is to only use arguments that exist in the official HashiCorp Google Provider v5.0 documentation. The `google_secret_manager_secret` resource does NOT have an argument named `automatic`. The replication policy is configured inside a `replication` block.
2.  **JSON OUTPUT ONLY:** Your entire response MUST be a single, valid JSON object.
3.  **File Paths are Keys:** The keys of the JSON object must be the full, relative file paths.
4.  **HCL Code is Value:** The values must be the HCL code content.
5.  **Directory Structure:** You MUST use the 'modules/' and 'environments/' structure.

---
**User Request:** "{user_request}"
Begin generation now.
"""

def generate_code_node(state: AgentState) -> dict:
    """Generates the initial code and resets the iteration counter."""
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
        # We now also initialize the iteration count
        return {"files_to_write": files_dict, "messages": [response], "iteration_count": 0}
    except json.JSONDecodeError as e:
        error_message = f"LLM response was not valid JSON. Error: {e}"
        return {"files_to_write": {}, "messages": [ToolMessage(content=error_message, tool_call_id="json_parser")], "iteration_count": 0}
    
def fix_code_node(state: AgentState) -> dict:
    """Takes the validation error and broken code, and asks the LLM to fix it."""
    print("--- ATTEMPTING TO FIX CODE ---")
    error_message = state['messages'][-1].content
    current_code_dict = state['files_to_write']
    code_as_string = json.dumps(current_code_dict, indent=2)

    # Building the prompt safely by joining a list of strings
    prompt_parts = [
        "You are a Terraform debugging expert. The Terraform configuration you last generated is invalid.",
        "Your task is to fix the error and provide a fully corrected version of the complete file structure as a single JSON object.",
        "\n**THE BROKEN CODE (JSON):**\n```json\n",
        code_as_string,
        "\n```\n",
        "\n**THE `terraform validate` ERROR:**\n```\n",
        error_message,
        "\n```\n",
        "\n**INSTRUCTION:**\nAnalyze the error and the broken code. The error explicitly states which argument is wrong.",
        "Remove the incorrect argument (`automatic = true`) from the resource `google_secret_manager_secret`.",
        "Then, return the complete, corrected code for ALL files as a single JSON object. Do not add any commentary."
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

        # Return the newly fixed code and increment the iteration counter
        return {
            "files_to_write": files_dict,
            "iteration_count": state['iteration_count'] + 1
        }
    except json.JSONDecodeError:
        return {"final_report": "Agent failed to fix the code and produced invalid JSON. Halting."}
    
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
                    with open(os.path.join(temp_dir, os.path.basename(file_path)), "w", encoding='utf-8') as f:
                        f.write(content)
            
            terraform_path = "C:\\Terraform\\terraform.exe"
            
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
            if os.path.exists(temp_dir):
                shutil.rmtree(temp_dir)

    return {"messages": [ToolMessage(content="Terraform code is valid.", tool_call_id="validator")]}

def file_writer_node(state: AgentState) -> dict:
    """Writes the final, validated files to disk."""
    print("--- 3. WRITING FILES TO DISK ---")
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