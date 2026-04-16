# my-terraform-agent/translator/yaml_engine.py

from .. import llm_provider
import re

def extract_yaml_blueprint(source_hcl, source_filename):
    """
    Phase 1: Converts specific Cloud HCL (e.g., GCP) into a generic,
    cloud-agnostic YAML representation of the infrastructure intent.
    """
    print(f"\n🧠 [Phase 1] Extracting Cloud-Agnostic Blueprint from '{source_filename}'...")

    prompt = (
        "You are a Senior Cloud Architect. Your task is to analyze the following Terraform HCL code "
        "and extract the fundamental infrastructure requirements into a generic, cloud-agnostic YAML format.\n\n"
        
        "CRITICAL INSTRUCTIONS:\n"
        "1.  **Strip Provider Syntax:** Do NOT include any provider-specific names (like 'google_compute_instance' or 'aws_instance'). Use generic terms like 'virtual_machine', 'database', or 'object_storage'.\n"
        "2.  **Abstract Values:** Convert specific machine types or zones into descriptive concepts. For example, instead of 'e2-medium', use 'size: medium_general_purpose'. Instead of 'us-central1-a', use 'location: us_central_zone_1'.\n"
        "3.  **Capture Intent, Not Code:** Focus on the business requirements (e.g., 'needs public IP', 'attached to default network', 'has read-only storage access').\n"
        "4.  **Format:** Output ONLY valid YAML. Do not include markdown fences (like ```yaml), comments, or explanations.\n\n"
        
        "--- SOURCE HCL CODE ---\n"
        f"{source_hcl}\n"
        "-----------------------\n"
    )

    try:
        print("   - Sending code to Translation Engine (LLM)...")
        # We use the text client because we want a raw YAML string, not a JSON object
        llm_client = llm_provider.get_llm_text_client()
        response = llm_client.invoke(prompt)
        
        yaml_output = response.content.strip()
        
        # Clean up markdown fences if the LLM ignores the instruction
        yaml_output = re.sub(r"^```yaml\s*", "", yaml_output, flags=re.IGNORECASE)
        yaml_output = re.sub(r"^```\s*", "", yaml_output, flags=re.IGNORECASE)
        yaml_output = re.sub(r"```\s*$", "", yaml_output).strip()

        if not yaml_output:
            print("   ❌ Extraction failed: LLM returned an empty response.")
            return None

        print("   ✅ Successfully extracted cloud-agnostic YAML blueprint.")
        return yaml_output

    except Exception as e:
        print(f"   ❌ An error occurred during YAML extraction: {e}")
        return None