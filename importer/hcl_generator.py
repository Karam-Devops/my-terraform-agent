# importer/hcl_generator.py

# This is a relative import. The '..' tells Python to go up one directory level
# (from 'importer' to 'my-terraform-agent') and then find the 'llm_provider' module.
from .. import llm_provider

def generate_hcl_from_json(resource_json_str, tf_type, hcl_name):
    """
    Constructs a prompt and uses the project's central llm_provider to generate HCL.
    """
    print("\n🤖 Calling LLM via the project's central llm_provider to generate HCL...")
    
    prompt = (
        "You are an expert Google Cloud and Terraform engineer. Based on the following JSON "
        f"configuration of a {tf_type} resource, generate the corresponding HCL resource block. "
        "The resource name in the HCL should be exactly '" + hcl_name + "'.\n\n"
        "Crucially, the output must be ONLY the HCL `resource` block itself. Do not include "
        "provider blocks, comments, ```hcl markdown fences, or any other explanatory text.\n\n"
        "JSON Configuration:\n"
        "```json\n"
        f"{resource_json_str}\n"
        "```"
    )

    # --- Integration Point ---
    # We now call your existing LLM provider.
    # I am assuming your llm_provider has a function named `invoke_model(prompt)`.
    #
    # !!! IMPORTANT !!!
    # If your function has a different name, please change `invoke_model` to match it.
    
    try:
        # Pass the detailed prompt to your existing LLM provider
        generated_hcl = llm_provider.invoke_model(prompt)

        if generated_hcl:
            # Clean up common LLM artifacts like markdown fences
            cleaned_hcl = generated_hcl.strip()
            if cleaned_hcl.startswith("```hcl"):
                cleaned_hcl = cleaned_hcl[5:].strip()
            if cleaned_hcl.endswith("```"):
                cleaned_hcl = cleaned_hcl[:-3].strip()
            
            print("   ✅ HCL generation successful.")
            return cleaned_hcl
        else:
            print("   ❌ LLM provider returned an empty or invalid response.")
            return None

    except Exception as e:
        print(f"   ❌ An error occurred while calling the llm_provider: {e}")
        # This could be an API key issue, network problem, etc.
        return None