# importer/hcl_generator.py

from .. import llm_provider

def generate_hcl_from_json(resource_json_str, tf_type, hcl_name, attempt, previous_error=None):
    """
    Generates raw HCL using a dedicated text client and a simplified, direct prompt.
    """
    print(f"\n🤖 Calling Text-Generation LLM (Attempt {attempt} of 5)...")

    if previous_error:
        # --- CORRECTION PROMPT (TEXT-ONLY) ---
        prompt = (
            "You are a Terraform expert correcting a previous error. The last attempt failed with this error:\n"
            f"--- ERROR ---\n{previous_error}\n--- END ERROR ---\n\n"
            "Your task is to regenerate the HCL code, fixing the error. "
            "Your ONLY output must be the raw HCL `resource` block. Do not add any other text.\n\n"
            "Original JSON Configuration:\n"
            "```json\n"
            f"{resource_json_str}\n"
            "```"
        )
    else:
        # --- INITIAL PROMPT (TEXT-ONLY) ---
        prompt = (
            "You are a silent Terraform code generator. Your ONLY job is to convert the provided JSON into a single, valid HCL `resource` block. "
            f"The resource type is '{tf_type}' and the name must be '{hcl_name}'.\n\n"
            "CRITICAL: Do not output any other text, comments, markdown, or explanations. Only the raw HCL code block.\n\n"
            "JSON Configuration:\n"
            "```json\n"
            f"{resource_json_str}\n"
            "```"
        )

    try:
        # --- THIS IS THE CORRECTED LOGIC ---
        
        # 1. Get the NEW client that is configured for text output.
        print("   - Getting Text LLM client...")
        llm_client = llm_provider.get_llm_text_client() # Use the new function

        # 2. Invoke the client. The result's content IS the HCL code.
        print("   - Sending prompt to LangChain client...")
        response = llm_client.invoke(prompt)
        generated_hcl = response.content

        if generated_hcl and "resource" in generated_hcl:
            # Clean up potential markdown fences, which can still appear
            cleaned_hcl = generated_hcl.strip().replace("```hcl", "").replace("```", "").strip()
            print("   ✅ Raw HCL generation successful.")
            return cleaned_hcl
        else:
            print("   ❌ LLM returned an empty or invalid response (did not contain 'resource').")
            return None

    except Exception as e:
        print(f"   ❌ An error occurred during the LLM process: {e}")
        return None