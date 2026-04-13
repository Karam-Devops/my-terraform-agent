# my-terraform-agent/importer/hcl_generator.py

from .. import llm_provider
from . import config

def generate_hcl_from_json(resource_json_str, tf_type, hcl_name, attempt, schema=None, previous_error=None):
    """
    Generates HCL with a hyper-specific prompt to enforce the correct local name.
    """
    print(f"\n🤖 Calling Text-Generation LLM (Attempt {attempt} of {config.MAX_LLM_RETRIES})...")

    # --- THIS IS THE FINAL AND MOST ROBUST PROMPT ---
    system_prompt = (
        "You are a precise Terraform HCL code generator. Your output must be a single, raw HCL resource block and nothing else.\n"
    )

    if previous_error:
        print("   - Self-correction mode activated.")
        system_prompt += f"\nIn the previous attempt, you failed with this error: `{previous_error}`. You must fix this error.\n"

    # Add the documentation context if available
    if schema and schema.get('arguments'):
        print("   - RAG mode activated. Injecting documentation schema into prompt.")
        valid_args = [arg['name'] for arg in schema['arguments']]
        system_prompt += (
            "\n--- DOCUMENTATION ---\n"
            f"Valid arguments for `{tf_type}` are: {', '.join(valid_args)}.\n"
            "Use only these arguments.\n"
            "--- END DOCUMENTATION ---\n"
        )
    
    # The final prompt with the critical naming instruction
    final_prompt = system_prompt + (
        "\n--- CRITICAL INSTRUCTIONS ---\n"
        "1.  The resource type MUST be `" + tf_type + "`.\n"
        "2.  The local name for the resource MUST be exactly `" + hcl_name + "`.\n"
        "3.  Your entire output must be ONLY the raw HCL code for this single resource block.\n\n"
        "--- TASK ---\n"
        "Generate the HCL for the following JSON:\n"
        "```json\n"
        f"{resource_json_str}\n"
        "```"
    )

    try:
        llm_client = llm_provider.get_llm_text_client()
        response = llm_client.invoke(final_prompt)
        generated_hcl = response.content

        # The robust validation check remains important
        if not generated_hcl or not generated_hcl.strip():
            print("   ❌ VALIDATION FAILED: LLM returned an empty response.")
            return None

        cleaned_hcl = generated_hcl.strip().replace("```hcl", "").replace("```", "").strip()
        expected_resource_line = f'resource "{tf_type}" "{hcl_name}"'
        
        if expected_resource_line not in cleaned_hcl:
            print(f"   ❌ VALIDATION FAILED: LLM output did not contain the required resource line: '{expected_resource_line}'")
            return None

        print(f"   ✅ HCL validation successful for '{hcl_name}'.")
        return cleaned_hcl

    except Exception as e:
        print(f"   ❌ An error occurred during the LLM process for '{hcl_name}': {e}")
        return None