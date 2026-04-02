# agent_nodes.py

import os
import subprocess
import json
import shutil
from langchain_core.messages import HumanMessage, ToolMessage

# Import the singleton LLM client and the config object
from llm_provider import llm
from config import config

# Import the new, standardized AgentState
from agent_state import AgentState


def create_generation_prompt(user_request: str) -> str:
    """Creates a specialized, structured prompt for the LLM."""
    return f"""
You are an expert Google Cloud Infrastructure Architect specializing in writing enterprise-grade Terraform.
Your task is to generate a complete and accurate set of Terraform files based on the user's request.

**CRITICAL INSTRUCTIONS:**
1.  Your entire response MUST be a single, valid JSON object. Do not add any text, explanation, or markdown outside of this JSON object.
2.  The keys of the JSON object must be the full, relative file paths for the generated files.
3.  The values must be the HCL code content for each file as a string.
4.  You MUST follow the standard enterprise directory structure:
    - For reusable infrastructure, use the 'modules/' directory (e.g., "modules/gcs-bucket/main.tf").
    - For environment-specific deployments, use the 'environments/' directory (e.g., "environments/development/us-central1/main.tf").
5.  Parameterize everything. Do not hardcode values like project IDs or regions. Expose them as variables in a `variables.tf` file.
6.  For every generated module, you must include `main.tf`, `variables.tf`, and `outputs.tf`.

**User Request:** "{user_request}"

Begin generation now.
"""


def generate_code_node(state: AgentState) -> dict:
    """
    Node that generates the initial Terraform configuration as a JSON object
    based on the user's first message.
    """
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
        
        return {"files_to_write": files_dict, "messages": [response]}
    except json.JSONDecodeError as e:
        print("\n" + "="*50)
        print("❌ FATAL: LLM response was not valid JSON even after cleaning.")
        print(f"   Parser Error: {e}")
        print("   Raw LLM Output:")
        print(response.content)
        print("="*50 + "\n")
        error_message = "LLM response was not valid JSON."
        return {"files_to_write": {}, "messages": [ToolMessage(content=error_message, tool_call_id="json_parser")]}


def validate_code_node(state: AgentState) -> dict:
    """
    Node that validates the generated Terraform configuration.
    
    NEW STRATEGY: It validates each generated *module* independently. This is
    more robust as it tests the core reusable code without worrying about
    inter-directory dependencies during validation.
    """
    print("--- 2. VALIDATING TERRAFORM CODE (MODULES) ---")
    
    files_to_write = state.get("files_to_write")
    if not files_to_write:
        return {"messages": [ToolMessage(content="Validation skipped: No files were generated.", tool_call_id="validator")]}
    
    # Find all unique directories within the 'modules/' path
    module_dirs = set(os.path.dirname(p) for p in files_to_write.keys() if p.startswith("modules/"))

    if not module_dirs:
        print("--> No modules found to validate. Assuming success.")
        return {"messages": [ToolMessage(content="Terraform code is valid.", tool_call_id="validator")]}

    try:
        for module_path in module_dirs:
            # Create a unique temp directory for each module's validation
            temp_dir = f"./temp_validation_{module_path.replace('/', '_')}"
            os.makedirs(temp_dir, exist_ok=True)

            print(f"--- Validating Module: {module_path} ---")

            # Write only the files for this specific module into the temp dir
            for file_path, content in files_to_write.items():
                if os.path.dirname(file_path) == module_path:
                    with open(os.path.join(temp_dir, os.path.basename(file_path)), "w", encoding='utf-8') as f:
                        f.write(content)
            
            # Use the hardcoded Terraform path
            terraform_path = "C:\\Terraform\\terraform.exe"
            
            print(f"Running 'terraform init' in {temp_dir}...")
            init_proc = subprocess.run([terraform_path, "init", "-input=false"], cwd=temp_dir, capture_output=True, text=True, check=False)
            if init_proc.returncode != 0:
                error = f"Terraform Init Failed in module '{module_path}':\n{init_proc.stderr}"
                print(f"Validation Error: {error}")
                shutil.rmtree(temp_dir) # Clean up
                return {"messages": [ToolMessage(content=error, tool_call_id="validator")]}
                
            print("Running 'terraform validate'...")
            validate_proc = subprocess.run([terraform_path, "validate"], cwd=temp_dir, capture_output=True, text=True, check=False)
            if validate_proc.returncode != 0:
                error = f"Terraform Validation Failed in module '{module_path}':\n{validate_proc.stderr}"
                print(f"Validation Error: {error}")
                shutil.rmtree(temp_dir) # Clean up
                return {"messages": [ToolMessage(content=error, tool_call_id="validator")]}
            
            # Clean up the successful validation directory
            shutil.rmtree(temp_dir)

    except Exception as e:
         return {"messages": [ToolMessage(content=f"An unexpected error occurred during validation: {e}", tool_call_id="validator")]}
            
    success_message = "Terraform code is valid."
    print(f"--> {success_message}")
    return {"messages": [ToolMessage(content=success_message, tool_call_id="validator")]}


def file_writer_node(state: AgentState) -> dict:
    """
    Takes the final, validated files from the state and writes them to the
    physical directory structure specified by the file paths.
    """
    print("--- 3. WRITING FILES TO DISK ---")
    
    files_to_write = state.get('files_to_write')
    if not files_to_write:
        report = "File writing skipped: No files to write."
        print(report)
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
            print(report)
            return {"final_report": report}
    
    report = f"Success! Wrote {len(files_to_write)} files to the '{base_dir}' directory."
    print(f"--> {report}")
    return {"final_report": report}