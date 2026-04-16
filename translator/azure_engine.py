# my-terraform-agent/translator/azure_engine.py

from .. import llm_provider
from . import config
import re

def generate_azure_hcl(yaml_blueprint, source_filename):
    """
    Converts the generic YAML blueprint into valid AzureRM HCL code.
    """
    print(f"\n🏗️  [Phase 2] Generating Azure HCL and Traceability Matrix...")

    prompt = (
        "You are an Expert Azure Cloud Architect. Your task is to write production-ready Terraform HCL "
        "for the `hashicorp/azurerm` provider based on the following generic infrastructure blueprint.\n\n"
        
        f"{config.AZURE_ARCHITECTURAL_RULES}\n\n"
        
        "CRITICAL OUTPUT FORMAT INSTRUCTIONS:\n"
        "Your output must consist of exactly TWO parts, formatted exactly as shown below.\n\n"
        
        "PART 1: THE TRACEABILITY MATRIX (Must be at the very top)\n"
        "You MUST include a multi-line comment block that explains how you mapped the generic concepts "
        "to specific Azure resources. Use this exact format:\n"
        "/*\n"
        "--- MULTI-CLOUD TRANSLATION TRACEABILITY MATRIX ---\n"
        "Blueprint Concept         | Target Azure Resource/Argument | Architectural Justification\n"
        "--------------------------------------------------------------------------------------\n"
        "[Concept 1]               | [azurerm_resource.name]        | [Brief explanation]\n"
        "--------------------------------------------------------------------------------------\n"
        "*/\n\n"
        
        "PART 2: THE TERRAFORM HCL CODE\n"
        "Below the Traceability Matrix, write all required `resource` or `data` blocks.\n"
        "Do NOT include a `provider` or `terraform` block.\n"
        "Do NOT wrap your entire output in markdown fences (like ```hcl).\n\n"
        
        "--- GENERIC INFRASTRUCTURE BLUEPRINT (YAML) ---\n"
        f"{yaml_blueprint}\n"
        "-----------------------------------------------\n"
    )

    try:
        print("   - Sending blueprint to Azure Generation Engine (LLM)...")
        llm_client = llm_provider.get_llm_text_client()
        response = llm_client.invoke(prompt)
        
        hcl_output = response.content.strip()
        
        # Clean up markdown fences
        hcl_output = re.sub(r"^```hcl\s*", "", hcl_output, flags=re.IGNORECASE)
        hcl_output = re.sub(r"^```terraform\s*", "", hcl_output, flags=re.IGNORECASE)
        hcl_output = re.sub(r"^```\s*", "", hcl_output, flags=re.IGNORECASE)
        hcl_output = re.sub(r"```\s*$", "", hcl_output).strip()

        if not hcl_output:
            print("   ❌ Generation failed: LLM returned an empty response.")
            return None

        print("   ✅ Successfully generated Azure HCL code.")
        return hcl_output

    except Exception as e:
        print(f"   ❌ An error occurred during Azure HCL generation: {e}")
        return None