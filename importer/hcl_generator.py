# my-terraform-agent/importer/hcl_generator.py

from .. import llm_provider
from . import config

def generate_hcl_from_json(resource_json_str, tf_type, hcl_name, attempt, schema=None, previous_error=None, expert_snippet=None, keys_to_omit=None, fields_to_ignore=None):
    """Generates HCL using a strictly separated, additive prompt architecture."""
    print(f"\n🤖 Calling Text-Generation LLM (Attempt {attempt} of {config.MAX_LLM_RETRIES})...")

    system_prompt = (
        "You are a precise Terraform HCL code generator. "
        "Your output must be a single, raw HCL resource block and nothing else.\n"
    )

    is_surgical_mode = False

    # 1. Lifecycle Ignore Changes (Appended via Python parsed lists)
    if fields_to_ignore:
        is_surgical_mode = True
        print(f"   - RAG mode: Instructing LLM to add lifecycle ignore_changes for: {fields_to_ignore}")
        ignore_list_str = ", ".join(f"{f}" for f in fields_to_ignore)
        system_prompt += (
            "\n\n========================================================================\n"
            "CRITICAL OVERRIDE - COMPUTED FIELD DIFFS DETECTED\n"
            "You MUST NOT define the following fields in the main resource arguments.\n"
            "Instead, you MUST add a `lifecycle` block at the end of the resource and add these field names to the `ignore_changes` list.\n"
            "Example:\n"
            "lifecycle {\n"
            f"  ignore_changes = [{ignore_list_str}]\n"
            "}\n"
            "========================================================================\n"
        )

    # 2. Expert HCL Snippets (Raw code injection)
    if expert_snippet:
        is_surgical_mode = True
        
        # Process IGNORE commands
        ignore_lines = [line for line in expert_snippet.splitlines() if line.startswith("IGNORE_LIST:")]
        if ignore_lines:
            fields_to_ignore = ignore_lines[0].replace("IGNORE_LIST:", "").split(",")
            fields_to_ignore = [f.strip() for f in fields_to_ignore if f.strip()]
            
            if fields_to_ignore:
                print(f"   - RAG mode activated: Instructing LLM to add lifecycle ignore_changes for: {fields_to_ignore}")
                ignore_list_str = ", ".join(f"{f}" for f in fields_to_ignore)
                system_prompt += (
                    "\n\n========================================================================\n"
                    "CRITICAL OVERRIDE - COMPUTED FIELD DIFFS DETECTED\n"
                    "Human experts have determined the following fields cause 'forces replacement' diffs.\n"
                    "You MUST NOT define these fields in the main resource arguments.\n"
                    "Instead, you MUST add a `lifecycle` block at the end of the resource and add these field names to the `ignore_changes` list.\n"
                    "Example:\n"
                    "lifecycle {\n"
                    f"  ignore_changes = [{ignore_list_str}]\n"
                    "}\n"
                    "========================================================================\n"
                )

        # Process standard OMIT/Expert Snippets
        if "OMIT" in expert_snippet:
            print("   - RAG mode activated: Enforcing negative constraints for read-only fields.")
        
        # --- THE DEFINITIVE FIX: Stronger Snippet Instructions ---
        # If the string contains anything other than OMIT or IGNORE_LIST commands, it's raw code.
        raw_snippets = [line for line in expert_snippet.splitlines() if not line.startswith("IGNORE_LIST:") and "OMIT" not in line]
        if raw_snippets:
             snippet_to_inject = "\n".join(raw_snippets).strip()
             if snippet_to_inject: # Only inject if there's actual code left
                 print("   - RAG mode activated: Using verified expert HCL snippet(s).")
                 system_prompt += (
                    "\n\n========================================================================\n"
                    "CRITICAL OVERRIDE - USE THIS EXACT CODE BLOCK\n"
                    "A human expert has provided the EXACT correct HCL syntax to fix a previous error.\n"
                    "You MUST include the following code block EXACTLY as written in your final output:\n"
                    f"```hcl\n{snippet_to_inject}\n```\n"
                    "*** CRITICAL RULE REGARDING DUPLICATION ***\n"
                    "If the JSON configuration contains data that corresponds to the block provided above, "
                    "you MUST use the provided expert block INSTEAD of generating your own.\n"
                    "DO NOT duplicate blocks. There must be ONLY ONE instance of this block type in your output.\n"
                    "========================================================================\n"
                 )

    # 3. Diff/Error Resolution (Only if not using surgical overrides)
    if previous_error and not is_surgical_mode:
        if "Terraform will perform the following actions" in previous_error or "execution plan" in previous_error:
            print("   - Context: State Drift / Diff Resolution.")
            system_prompt += (
                "\n\n========================================================================\n"
                "STATE DRIFT DETECTED (TERRAFORM PLAN DIFF)\n"
                "Your previous HCL was syntactically valid, but it does not match the live cloud resource.\n"
                "Here is the `terraform plan` diff:\n"
                f"```diff\n{previous_error}\n```\n\n"
                "CRITICAL RULES FOR RESOLVING DIFFS:\n"
                "1. `+ field = \"value\"`: Field is MISSING from your HCL. ADD it.\n"
                "2. `- field = \"value\" -> null`: Your HCL omitted it. ADD it back.\n"
                "3. `~ field = \"old\" -> \"new\"`: Your HCL value is wrong. UPDATE it to match the 'old' value on the left.\n"
                "4. `- list_field = [] -> null`: Remove the empty `[]` assignment completely.\n"
                "5. IGNORE computed attributes (`id`, `self_link`, `creation_timestamp`, etc.).\n"
                "========================================================================\n"
            )
        else:
            print("   - Context: Syntax / Schema Error.")
            system_prompt += f"\n\nIn the previous attempt, you failed with this syntax error: `{previous_error}`. You must fix this error."

    # 4. Strict Negative Constraints (OMIT keys)
    if keys_to_omit:
        print(f"   - RAG mode: Applying negative constraints for omitted keys: {keys_to_omit}")
        system_prompt += (
            "\n\n--- STRICT NEGATIVE CONSTRAINTS ---\n"
            "You MUST NOT generate, include, or mention the following arguments in your HCL, "
            "even if you think they are required or default values. They are strictly forbidden:\n"
            f"FORBIDDEN ARGUMENTS: {', '.join(keys_to_omit)}\n"
            "-----------------------------------\n"
        )

    # 5. Schema Validation
    if schema and schema.get('arguments'):
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

        print("\n   [DEBUG] Raw LLM HCL Output:")
        print("   " + "-"*40)
        print(generated_hcl)
        print("   " + "-"*40 + "\n")

        if not generated_hcl or not generated_hcl.strip(): 
            print("   ❌ VALIDATION FAILED: LLM returned an empty response.")
            return None
            
        cleaned_hcl = generated_hcl.strip().replace("```hcl", "").replace("```", "").strip()
        if f'resource "{tf_type}" "{hcl_name}"' not in cleaned_hcl:
            print(f"   ❌ VALIDATION FAILED: LLM output did not contain the required resource line.")
            return None

        print(f"   ✅ HCL validation successful for '{hcl_name}'.")
        return cleaned_hcl
    except Exception as e:
        print(f"   ❌ An error occurred during the LLM process: {e}")
        return None