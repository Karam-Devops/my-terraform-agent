# my-terraform-agent/importer/hcl_generator.py

from common.logging import get_logger

from .. import llm_provider
from . import config
from . import post_llm_overrides
from .schema_prompt import build_schema_summary

_log = get_logger(__name__)

# Cap raw LLM HCL emitted at DEBUG level. Without a cap a single response
# can easily be 5-30KB and would dominate log lines / Cloud Logging cost.
# Operators that need the full body should re-run with the LLM provider's
# native trace logging on.
_DEBUG_HCL_TRUNCATE = 500


def generate_hcl_from_json(resource_json_str, tf_type, hcl_name, attempt, schema=None, previous_error=None, expert_snippet=None, keys_to_omit=None, fields_to_ignore=None, mode_addendum=None):
    """Generates HCL using a strictly separated, additive prompt architecture."""
    _log.info(
        "llm_invoke_start",
        tf_type=tf_type,
        hcl_name=hcl_name,
        attempt=attempt,
        max_retries=config.MAX_LLM_RETRIES,
    )

    system_prompt = (
        "You are a precise Terraform HCL code generator. "
        "Your output must be a single, raw HCL resource block and nothing else.\n"
    )

    is_surgical_mode = False

    # 1. Lifecycle Ignore Changes (Appended via Python parsed lists)
    if fields_to_ignore:
        is_surgical_mode = True
        _log.info(
            "rag_mode_activated",
            source="fields_to_ignore_arg",
            tf_type=tf_type,
            fields=list(fields_to_ignore),
        )
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
                _log.info(
                    "rag_mode_activated",
                    source="expert_snippet_ignore_list",
                    tf_type=tf_type,
                    fields=list(fields_to_ignore),
                )
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
            _log.info(
                "rag_mode_activated",
                source="expert_snippet_omit",
                tf_type=tf_type,
            )
        
        # --- THE DEFINITIVE FIX: Stronger Snippet Instructions ---
        # If the string contains anything other than OMIT or IGNORE_LIST commands, it's raw code.
        raw_snippets = [line for line in expert_snippet.splitlines() if not line.startswith("IGNORE_LIST:") and "OMIT" not in line]
        if raw_snippets:
             snippet_to_inject = "\n".join(raw_snippets).strip()
             if snippet_to_inject: # Only inject if there's actual code left
                 _log.info(
                     "rag_mode_activated",
                     source="expert_hcl_snippet",
                     tf_type=tf_type,
                 )
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
            _log.info(
                "error_context_detected",
                tf_type=tf_type,
                context="state_drift_diff",
            )
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
            _log.info(
                "error_context_detected",
                tf_type=tf_type,
                context="syntax_or_schema_error",
            )
            system_prompt += f"\n\nIn the previous attempt, you failed with this syntax error: `{previous_error}`. You must fix this error."

    # 4. Strict Negative Constraints (OMIT keys)
    if keys_to_omit:
        _log.info(
            "rag_mode_activated",
            source="negative_constraints",
            tf_type=tf_type,
            omitted_keys=list(keys_to_omit),
        )
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

        # DEBUG-only: capped slice of the raw response. Set
        # MTAGENT_LOG_LEVEL=DEBUG to see this. See _DEBUG_HCL_TRUNCATE
        # for the truncation rationale.
        if generated_hcl:
            _log.debug(
                "llm_raw_output",
                tf_type=tf_type,
                hcl_name=hcl_name,
                length=len(generated_hcl),
                preview=generated_hcl[:_DEBUG_HCL_TRUNCATE],
                truncated=len(generated_hcl) > _DEBUG_HCL_TRUNCATE,
            )

        if not generated_hcl or not generated_hcl.strip():
            _log.error(
                "hcl_validation_failed",
                tf_type=tf_type,
                hcl_name=hcl_name,
                reason="empty_llm_response",
            )
            return None

        cleaned_hcl = generated_hcl.strip().replace("```hcl", "").replace("```", "").strip()

        # Deterministic post-pass to fix known LLM hallucinations of provider
        # field names (see importer/post_llm_overrides.py for rationale). Runs
        # before the resource-line check so the validator sees the corrected
        # text. Fail-OPEN: if the override layer errors internally it returns
        # the input unchanged with an empty corrections list.
        cleaned_hcl, corrections = post_llm_overrides.apply_overrides(tf_type, cleaned_hcl)
        for desc in corrections:
            _log.info(
                "post_llm_correction_applied",
                tf_type=tf_type,
                description=desc,
            )

        if f'resource "{tf_type}" "{hcl_name}"' not in cleaned_hcl:
            _log.error(
                "hcl_validation_failed",
                tf_type=tf_type,
                hcl_name=hcl_name,
                reason="missing_resource_line",
            )
            return None

        _log.info(
            "hcl_validation_ok",
            tf_type=tf_type,
            hcl_name=hcl_name,
        )
        return cleaned_hcl
    except Exception as e:
        _log.error(
            "llm_generation_failed",
            tf_type=tf_type,
            hcl_name=hcl_name,
            error_type=type(e).__name__,
            error=str(e),
        )
        return None