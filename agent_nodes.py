# agent_nodes.py

import os
import subprocess
import uuid
import shutil
import json
from langchain_core.messages import HumanMessage

# --- THE ABSTRACTION FIREWALL ---
# We import our provider function, not a specific LLM class.
from llm_provider import get_llm_client

# --- NO LONGER NEEDED HERE ---
# from config import GEMINI_MODEL, GCP_PROJECT_ID

from agent_state import AgentState

# We get the LLM client from our provider, without knowing its specific class.
llm = get_llm_client()

def draft_code_node(state: AgentState) -> dict:
    """Node that drafts the initial modular Terraform configuration as a JSON object."""
    print("--- DRAFTING MODULAR TERRAFORM CODE ---")
    
    system_prompt = """You are an expert Google Cloud Infrastructure Architect. Your purpose is to generate a complete, modular, and parameterized Terraform configuration as a single, valid JSON object.
**CRITICAL INSTRUCTIONS:**
1.  Your entire response MUST be a single, valid JSON object. The keys must be filenames (`main.tf`, `variables.tf`, `outputs.tf`, `terraform.tfvars.example`), and the values must be the code content for each file as a string.
2.  **DO NOT** add any text, explanation, or markdown outside of the final JSON object.
3.  **Parameterize Everything:** Do not hardcode values. Expose them as variables in `variables.tf`.
4.  **Create Sensible Variables:** In `variables.tf`, define every variable with a `type`, `description`, and a reasonable `default` value.
5.  **Provide Outputs:** In `outputs.tf`, expose important resource attributes."""
    
    prompt = f"User Request: Generate the complete Terraform module (as a JSON object) for: '{state['user_prompt']}'"
    message = HumanMessage(content=f"{system_prompt}\n\n{prompt}")
    response = llm.invoke([message])
    
    try:
        # This block is complete and correct
        cleaned_response = response.content.strip()
        if cleaned_response.startswith("```json"):
            cleaned_response = cleaned_response[7:-3].strip()
        
        files_dict = json.loads(cleaned_response)
        return {"terraform_files": files_dict, "iteration_count": 0}
    except json.JSONDecodeError:
        error_message = "LLM response was not valid JSON. Response:\n" + response.content
        return {"validation_error": error_message, "terraform_files": {}}

def validate_code_node(state: AgentState) -> dict:
    """Node that validates the generated multi-file Terraform configuration."""
    print("--- VALIDATING TERRAFORM CODE ---")
    temp_dir = f"./temp_terraform_{uuid.uuid4()}"
    os.makedirs(temp_dir, exist_ok=True)
    
    try:
        files = state.get("terraform_files")
        if not files or not isinstance(files, dict):
            return {"validation_error": "Agent failed to generate any files."}
        
        for filename, content in files.items():
            with open(os.path.join(temp_dir, filename), "w") as f:
                f.write(content)

        terraform_path = "C:\\Terraform\\terraform.exe"
        init_proc = subprocess.run([terraform_path, "init", "-input=false"], cwd=temp_dir, capture_output=True, text=True, check=False)
        if init_proc.returncode != 0: return {"validation_error": init_proc.stderr}
            
        validate_proc = subprocess.run([terraform_path, "validate"], cwd=temp_dir, capture_output=True, text=True, check=False)
        if validate_proc.returncode != 0: return {"validation_error": validate_proc.stderr}
    finally:
        if os.path.exists(temp_dir): shutil.rmtree(temp_dir)
            
    print("--> Validation successful!")
    return {"validation_error": ""}

def fix_code_node(state: AgentState) -> dict:
    """Node that attempts to fix the multi-file configuration based on an error."""
    print("--- ATTEMPTING TO FIX CODE ---")
    files_as_string = "\n\n".join([f"--- {name} ---\n{content}" for name, content in state["terraform_files"].items()])

    prompt = f"""The Terraform configuration you generated is invalid. Here are the files:
{files_as_string}
---
The `terraform` command failed with this error:
--- ERROR ---
{state['validation_error']}
---
Analyze the error and all the files, then provide a fully corrected version of the complete file structure as a single JSON object. Do not add commentary."""
    
    message = HumanMessage(content=prompt)
    response = llm.invoke([message])
    
    try:
        # --- THIS IS THE CORRECTED BLOCK ---
        # The line defining `cleaned_response` is now correctly included.
        cleaned_response = response.content.strip()
        if cleaned_response.startswith("```json"):
            cleaned_response = cleaned_response[7:-3].strip()
            
        files_dict = json.loads(cleaned_response)
        return {"terraform_files": files_dict, "iteration_count": state['iteration_count'] + 1}
    except json.JSONDecodeError:
        error_message = "LLM response during fix was not valid JSON. Response:\n" + response.content
        return {"validation_error": error_message}