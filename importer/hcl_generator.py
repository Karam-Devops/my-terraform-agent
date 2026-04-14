# my-terraform-agent/importer/hcl_generator.py

from .. import llm_provider
from . import config

def generate_hcl_from_json(resource_json_str, tf_type, hcl_name, attempt, schema=None, previous_error=None, expert_example=None):
    """
    Generates HCL, prioritizing expert examples from the RAG system if provided.
    """
    print(f"\n🤖 Calling Text-Generation LLM (Attempt {attempt} of {config.MAX_LLM_RETRIES})...")

    # This prompt structure is now final and prioritizes the best information first.
    system_prompt = "You are a precise Terraform HCL code generator. Your output must be a single, raw HCL resource block and nothing else."

    if expert_example:
        print("   - RAG mode activated: Using a verified expert example.")
        system_prompt += (
            "\n\n--- EXPERT RULE (MUST FOLLOW) ---\n"
            "You have previously failed on a similar task. A human expert has provided the following correct HCL snippet. "
            "You MUST use this exact pattern to structure the corresponding part of your output.\n"
            f"```hcl\n{expert_example}\n```\n"
            "--- END EXPERT RULE ---"
        )
    elif previous_error:
        print("   - Self-correction mode activated (no expert example found).")
        system_prompt += f"\n\nIn the previous attempt, you failed with this error: `{previous_error}`. You must fix this error."
    
    if schema and schema.get('arguments'):
        # Documentation is still useful as a fallback and for general structure
        valid_args = [arg['name'] for arg in schema['arguments']]
        system_prompt += f"\n\nValid arguments for `{tf_type}` are: {', '.join(valid_args)}."

    final_prompt = system_prompt + (
        f"\n\n--- TASK ---\n"
        f"Generate the resource block. The type must be `{tf_type}` and the local name must be `{hcl_name}`.\n"
        "Convert this JSON:\n"
        "```json\n" + resource_json_str + "\n```"
    )

    try:
        llm_client = llm_provider.get_llm_text_client()
        response = llm_client.invoke(final_prompt)
        generated_hcl = response.content
        
        # Validation
        if not generated_hcl or not generated_hcl.strip(): return None
        cleaned_hcl = generated_hcl.strip().replace("```hcl", "").replace("```", "").strip()
        if f'resource "{tf_type}" "{hcl_name}"' not in cleaned_hcl: return None

        print(f"   ✅ HCL generation successful for '{hcl_name}'.")
        return cleaned_hcl
    except Exception as e:
        print(f"   ❌ An error occurred during the LLM process: {e}")
        return None