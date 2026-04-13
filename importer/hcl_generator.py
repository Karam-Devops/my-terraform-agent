# my-terraform-agent/importer/hcl_generator.py

from .. import llm_provider
from . import config # Import config to use MAX_LLM_RETRIES

def generate_hcl_from_json(resource_json_str, tf_type, hcl_name, attempt, schema=None, previous_error=None):
    """
    Generates HCL, augmenting the prompt with the official documentation schema if provided.
    It also handles correction attempts by including past errors.
    """
    print(f"\n🤖 Calling Text-Generation LLM (Attempt {attempt} of {config.MAX_LLM_RETRIES})...")

    # --- RAG PROMPT: The most powerful version ---
    # Build the prompt dynamically based on the context (initial attempt, correction, or RAG-powered correction).
    
    system_prompt = (
        "You are a Terraform engineer. Your task is to convert a JSON object into a single, valid HCL resource block. "
        "Your output MUST be only the raw HCL code. Do not add comments, markdown, or explanations.\n"
    )

    if previous_error:
        print("   - Self-correction mode activated.")
        system_prompt += f"\nIn the previous attempt, you failed with this error: `{previous_error}`. You must fix this error.\n"

    if schema and schema.get('arguments'):
        print("   - RAG mode activated. Injecting documentation schema into prompt.")
        valid_args = [arg['name'] for arg in schema['arguments']]
        system_prompt += (
            "\n--- OFFICIAL DOCUMENTATION (Source of Truth) ---\n"
            f"The resource type MUST be `{tf_type}`.\n"
            f"According to the documentation, the ONLY valid arguments for this resource are: {', '.join(valid_args)}.\n"
            "You MUST NOT use any arguments other than these. Pay close attention to nested block requirements.\n"
            "--- END OF DOCUMENTATION ---\n"
        )
    else:
        print("   - Proceeding without documentation context.")
        system_prompt += f"The target resource type is `{tf_type}`.\n"

    final_prompt = system_prompt + (
        "\n--- TASK ---\n"
        "Now, convert the following JSON into a single, valid HCL resource block:\n"
        "```json\n"
        f"{resource_json_str}\n"
        "```"
    )

    try:
        print("   - Getting Text LLM client...")
        llm_client = llm_provider.get_llm_text_client()

        print("   - Sending prompt to LangChain client...")
        response = llm_client.invoke(final_prompt)
        generated_hcl = response.content

        if generated_hcl and "resource" in generated_hcl:
            cleaned_hcl = generated_hcl.strip().replace("```hcl", "").replace("```", "").strip()
            print(f"   ✅ Raw HCL generation successful for '{hcl_name}'.")
            return cleaned_hcl
        else:
            print(f"   ❌ LLM returned an empty or invalid response for '{hcl_name}'.")
            return None

    except Exception as e:
        print(f"   ❌ An error occurred during the LLM process for '{hcl_name}': {e}")
        return None