# importer/hcl_generator.py

import json
from .. import llm_provider

def generate_hcl_from_json(resource_json_str, tf_type, hcl_name):
    """
    Constructs a highly specific "one-shot" prompt with an example to guide the LLM,
    then generates and cleans the HCL.
    """
    print("\n🤖 Calling LLM with an advanced one-shot example prompt...")
    
    # --- THIS IS THE MOST ADVANCED PROMPT WITH A CONCRETE EXAMPLE ---
    prompt = (
        "You are a world-class Google Cloud and Terraform engineer. Your primary skill is converting a resource's JSON representation into perfect, schema-compliant HCL. "
        "Your task is to generate a Terraform HCL resource block.\n\n"
        f"The Terraform resource type is '{tf_type}' and the name must be '{hcl_name}'.\n\n"
        "--- CRITICAL RULES & EXAMPLES ---\n"
        "1.  You MUST translate nested JSON objects into proper HCL blocks. Do not flatten them into simple arguments. This is the most common and critical error to avoid.\n\n"
        "    **EXAMPLE:**\n"
        "    - GIVEN the JSON snippet: `\"softDeletePolicy\": {\"retentionDuration\": \"604800s\"}`\n"
        "    - **INCORRECT HCL:** `retention_duration = \"604800s\"`\n"
        "    - **CORRECT HCL:** `soft_delete_policy { retention_duration_seconds = 604800 }`\n"
        "    Follow this correct pattern for all nested objects.\n\n"
        "2.  The final output MUST be a valid JSON object, containing a single key named 'hcl_code', where the value is the complete HCL code as a string.\n"
        "3.  Do not include comments, markdown, provider blocks, or any other explanatory text in the final HCL code string.\n\n"
        "--- TASK ---\n"
        "Now, perform this conversion for the following JSON configuration:\n"
        "```json\n"
        f"{resource_json_str}\n"
        "```"
    )

    try:
        print("   - Getting shared LLM client...")
        llm_client = llm_provider.get_llm_client()

        print("   - Sending prompt to LangChain client...")
        response = llm_client.invoke(prompt)

        raw_response_str = response.content
        print("   - Received response from LLM.")
        
        print("   - Cleaning raw response string...")
        cleaned_json_str = raw_response_str.strip()
        if cleaned_json_str.startswith("```json"):
            cleaned_json_str = cleaned_json_str[7:].strip()
        if cleaned_json_str.endswith("```"):
            cleaned_json_str = cleaned_json_str[:-3].strip()

        print("   - Parsing cleaned JSON string...")
        response_data = json.loads(cleaned_json_str)
        generated_hcl = response_data.get("hcl_code")
        
        if generated_hcl:
            print("   ✅ HCL generation successful.")
            return generated_hcl.strip()
        else:
            print("   ❌ LLM JSON response was missing the 'hcl_code' key.")
            return None

    except json.JSONDecodeError as e:
        print(f"   ❌ After cleaning, the response was still not valid JSON. Error: {e}")
        print(f"   [DEBUG] The string we tried to parse was: '{cleaned_json_str}'")
        return None
    except Exception as e:
        print(f"   ❌ An unhandled error occurred during the LLM process: {e}")
        return None