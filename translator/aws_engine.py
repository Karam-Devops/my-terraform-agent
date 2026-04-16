# my-terraform-agent/translator/aws_engine.py

from .. import llm_provider
from . import config
import re

def generate_aws_hcl(yaml_blueprint, source_filename):
    """
    Phase 2: Converts the generic YAML blueprint into valid AWS HCL code,
    incorporating specific architectural rules and a Traceability Matrix.
    """
    print(f"\n🏗️  [Phase 2] Generating AWS HCL and Traceability Matrix...")

    prompt = (
        "You are an Expert AWS Cloud Architect. Your task is to write production-ready Terraform HCL "
        "for the AWS provider based on the following generic infrastructure blueprint.\n\n"
        
        f"{config.AWS_ARCHITECTURAL_RULES}\n\n"
        
        "CRITICAL OUTPUT FORMAT INSTRUCTIONS:\n"
        "Your output must consist of exactly TWO parts, formatted exactly as shown below.\n\n"
        
        "PART 1: THE TRACEABILITY MATRIX (Must be at the very top)\n"
        "You MUST include a multi-line comment block that explains how you mapped the generic concepts "
        "to specific AWS resources. Use this exact format:\n"
        "/*\n"
        "--- MULTI-CLOUD TRANSLATION TRACEABILITY MATRIX ---\n"
        "Blueprint Concept         | Target AWS Resource/Argument | Architectural Justification\n"
        "--------------------------------------------------------------------------------------\n"
        "[Concept 1]               | [aws_resource.name]          | [Brief explanation]\n"
        "[Concept 2]               | [aws_resource.argument]      | [Brief explanation]\n"
        "--------------------------------------------------------------------------------------\n"
        "*/\n\n"
        
        "PART 2: THE TERRAFORM HCL CODE\n"
        "Below the Traceability Matrix, write all required `resource` or `data` blocks.\n"
        "Do NOT include a `provider` or `terraform` block. Output only the resource definitions.\n"
        "Do NOT wrap your entire output in markdown fences (like ```hcl).\n\n"
        
        "--- GENERIC INFRASTRUCTURE BLUEPRINT (YAML) ---\n"
        f"{yaml_blueprint}\n"
        "-----------------------------------------------\n"
    )

    try:
        print("   - Sending blueprint to AWS Generation Engine (LLM)...")
        llm_client = llm_provider.get_llm_text_client()
        response = llm_client.invoke(prompt)
        
        aws_hcl_output = response.content.strip()
        
        # Clean up markdown fences if the LLM ignores the instruction
        aws_hcl_output = re.sub(r"^```hcl\s*", "", aws_hcl_output, flags=re.IGNORECASE)
        aws_hcl_output = re.sub(r"^```terraform\s*", "", aws_hcl_output, flags=re.IGNORECASE)
        aws_hcl_output = re.sub(r"^```\s*", "", aws_hcl_output, flags=re.IGNORECASE)
        aws_hcl_output = re.sub(r"```\s*$", "", aws_hcl_output).strip()

        if not aws_hcl_output:
            print("   ❌ Generation failed: LLM returned an empty response.")
            return None

        # Basic check to ensure the Traceability Matrix is present
        if "TRACEABILITY MATRIX" not in aws_hcl_output.upper():
            print("   ⚠️  Warning: The LLM failed to include the required Traceability Matrix.")

        print("   ✅ Successfully generated AWS HCL code.")
        return aws_hcl_output

    except Exception as e:
        print(f"   ❌ An error occurred during AWS HCL generation: {e}")
        return None