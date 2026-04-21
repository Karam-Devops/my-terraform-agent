# my-terraform-agent/importer/hcl_generator.py

from .. import llm_provider
from . import config
from .schema_prompt import build_schema_summary


def generate_hcl_from_json(resource_json_str, tf_type, hcl_name, attempt, schema=None, previous_error=None, expert_snippet=None, keys_to_omit=None, fields_to_ignore=None, mode_addendum=None):
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
            "CRITICAL OVERRIDE - LIFECYCLE IGNORE_CHANGES REQUIRED\n"
            "The following fields are optional+computed: the cloud has a value, but the\n"
            "provider may recompute it later. Handle them as follows:\n"
            "  1. WRITE each field in the resource body using the value from the input JSON\n"
            "     (Terraform needs the configured value at plan time - omitting it can break\n"
            "      `Read` for fields like `zone`, `region`, `location`, `project`).\n"
            "  2. ALSO add the field name to a `lifecycle.ignore_changes` block so future\n"
            "     drift on that field is suppressed.\n"
            "  3. `ignore_changes` entries are UNQUOTED identifier references, never\n"
            "     strings. Correct: `ignore_changes = [zone, labels]`. WRONG:\n"
            "     `ignore_changes = [\"zone\", \"labels\"]` (emits a deprecation warning).\n"
            f"FIELDS: {', '.join(fields_to_ignore)}\n"
            "Example shape:\n"
            "  zone = \"us-central1-a\"\n"
            "  ...\n"
            "  lifecycle {\n"
            f"    ignore_changes = [{ignore_list_str}]\n"
            "  }\n"
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
                    "CRITICAL OVERRIDE - LIFECYCLE IGNORE_CHANGES REQUIRED (heuristic)\n"
                    "Human experts flagged these fields as causing perpetual / replacement diffs.\n"
                    "  1. WRITE each field with its value from the input JSON.\n"
                    "  2. ALSO add the field name to a `lifecycle.ignore_changes` block so\n"
                    "     future provider-side recomputes are suppressed.\n"
                    "  3. `ignore_changes` entries are UNQUOTED identifier references, never\n"
                    "     strings. Correct: `ignore_changes = [zone, labels]`. WRONG:\n"
                    "     `ignore_changes = [\"zone\", \"labels\"]` (emits a deprecation warning).\n"
                    "Do NOT omit the field entirely - that breaks Read for things like\n"
                    "`zone` / `region` / `location` / `project`.\n"
                    f"FIELDS: {', '.join(fields_to_ignore)}\n"
                    "Example shape:\n"
                    "  lifecycle {\n"
                    f"    ignore_changes = [{ignore_list_str}]\n"
                    "  }\n"
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
                "1. `+ field = \"value\"`: Field is MISSING from your HCL. ADD it with that value.\n"
                "2. `- field = \"value\" -> null`: Your HCL omitted it. ADD it back with that value.\n"
                "3. `~ field = \"X\" -> null`: Your HCL omitted it; the cloud has \"X\". ADD `field = \"X\"` (the LEFT side is the cloud value, the RIGHT is what your HCL implies).\n"
                "4. `~ field = \"old\" -> \"new\"`: Your HCL value is wrong. UPDATE it to match the 'old' (left) value, which is what the cloud has.\n"
                "5. Diffs inside a nested block (e.g. `~ scheduling { ... }` with a `~ instance_termination_action` line inside) mean you MUST add that field INSIDE the same nested block in your HCL, not at the top level.\n"
                "6. `- list_field = [] -> null`: Remove the empty `[]` assignment completely.\n"
                "7. IGNORE computed attributes (`id`, `self_link`, `creation_timestamp`, etc.) - they are framework / read-only fields that should never appear in HCL.\n"
                "8. Do NOT remove fields from the existing HCL unless the diff explicitly says to. Preserve every line that is not contradicted by the diff.\n"
                "9. `lifecycle.ignore_changes` entries must be UNQUOTED identifiers: `[zone, labels]` not `[\"zone\", \"labels\"]`.\n"
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

    # 5. Schema injection (PR-5: full structure, not just arg names)
    if schema:
        schema_block = build_schema_summary(tf_type, schema)
        if schema_block:
            system_prompt += schema_block

    # 6. Resource-mode addendum (PR-10: e.g. GKE Autopilot constraints)
    # Goes AFTER the schema summary so it overrides the OPTIONAL BLOCKS
    # listing — the LLM should treat mode constraints as authoritative.
    if mode_addendum:
        system_prompt += mode_addendum

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