# my-terraform-agent/translator/yaml_engine.py

import os
import re
import logging
from typing import Optional
from langchain_core.messages import SystemMessage, HumanMessage
from .. import llm_provider

# Initialize standard logger for enterprise observability
logger = logging.getLogger(__name__)

def extract_yaml_blueprint(source_hcl: str, source_filename: str) -> Optional[str]:
    """
    Phase 1: Converts specific Cloud HCL (e.g., GCP) into a generic,
    cloud-agnostic YAML representation of the infrastructure intent.
    """
    logger.info(f"🧠 [Phase 1] Extracting Cloud-Agnostic Blueprint from '{source_filename}'...")

    # System prompt: Defines the persona, rules, and expected format.
    system_instruction = (
        "You are a Senior Cloud Architect. Your task is to analyze the provided Terraform HCL code "
        "and extract the fundamental infrastructure requirements into a generic, cloud-agnostic YAML format.\n\n"
        
        "CRITICAL INSTRUCTIONS:\n"
        "1. **FAITHFULNESS — DO NOT EMBELLISH (highest priority rule):** Your output must reflect ONLY what is literally present in the source HCL. Never add, infer, upgrade, or 'improve' configuration that isn't explicitly stated. Translation is not a security review or a best-practices audit. Specifically:\n"
        "   - **Preserve disabled state.** If the source explicitly disables a feature (e.g., `enabled = false`, `state = \"DECRYPTED\"`, `mode = \"DISABLED\"`, `vulnerability_mode = \"VULNERABILITY_DISABLED\"`), the YAML MUST preserve that disabled state. Do NOT silently flip it to enabled.\n"
        "   - **Absence means absent.** If the source OMITS a configuration block, the corresponding capability is off / default. Do NOT invent a configured state. Concrete example: the absence of a `private_cluster_config` block means the cluster is PUBLIC — do NOT mark it as private. The absence of an `encryption` block means encryption is NOT configured — do NOT add it.\n"
        "   - **Never upgrade weaker options.** If a value is set to a 'weaker' option (e.g., `public_access_prevention = \"inherited\"`, `state = \"DECRYPTED\"`, `instance_termination_action = \"STOP\"`, `release_channel = \"REGULAR\"`), preserve that exact option. Never substitute a stricter, safer, or 'more modern' equivalent.\n"
        "   - **Never add security features the source didn't request.** No KMS keys, no private endpoints, no encryption-at-rest, no network isolation, no audit logging unless the source HCL explicitly configures them.\n"
        "2. **Strip Provider Syntax — but preserve meaning-changing qualifiers:** Do NOT include provider-specific RESOURCE TYPE names (like 'google_compute_instance' or 'aws_instance'); use generic terms like 'virtual_machine', 'database', or 'object_storage'. HOWEVER, when an ARGUMENT name contains a provider qualifier that changes its meaning, you MUST preserve that qualifier in the YAML key. Concrete example: `gcp_public_cidrs_access_enabled` is specifically about Google's own public CIDR ranges accessing the cluster API — it is NOT a generic 'is the endpoint public/private' switch. Emit it as `gcp_public_cidrs_access_enabled: false`, NOT as `public_access_enabled: false`. Generalizing meaning-bearing qualifiers strips information the downstream stage needs to translate correctly.\n"
        "3. **Abstract Values:** Convert specific machine types or zones into descriptive concepts. For example, instead of 'e2-medium', use 'size: medium_general_purpose'. Instead of 'us-central1-a', use 'location: us_central_zone_1'.\n"
        "4. **Document, Don't Speculate:** Record the requirements actually expressed in the source. Do NOT speculate about what the user 'probably wants' or what would be a 'best practice'. If a field is unusual or weak, faithfully record it as-is — downstream stages will handle target-cloud equivalence.\n"
        "5. **Preserve `lifecycle.ignore_changes` as a behavioral contract.** If the source resource contains a `lifecycle { ignore_changes = [...] }` block, emit those fields under a top-level `behavioral_overrides:` key in the YAML, like this:\n"
        "       behavioral_overrides:\n"
        "         ignore_changes:\n"
        "           - field_name_1\n"
        "           - field_name_2\n"
        "   This is an operator contract — the user has explicitly told Terraform to stop noticing drift on these fields. That intent MUST round-trip through translation into the target HCL. Do NOT silently drop the lifecycle block.\n"
        "6. **Format:** Output ONLY valid YAML. Do not include markdown fences (like ```yaml), comments, or explanations.\n"
    )

    # Human prompt: Provides the actual data payload.
    human_instruction = (
        "--- SOURCE HCL CODE ---\n"
        f"{source_hcl}\n"
        "-----------------------\n"
    )

    try:
        logger.info("   - Sending code to Translation Engine (Gemini)...")
        llm_client = llm_provider.get_llm_text_client()
        
        # Using structured messages (System + Human) for optimal Gemini 2.5 Pro performance
        messages = [
            SystemMessage(content=system_instruction),
            HumanMessage(content=human_instruction)
        ]
        
        response = llm_client.invoke(messages)
        yaml_output = response.content.strip()

        if not yaml_output:
            logger.error("   ❌ Extraction failed: LLM returned an empty response.")
            return None

        # Robust cleanup: Remove markdown fences gracefully
        # Matches ```yaml or ``` at the start, and ``` at the end.
        yaml_output = re.sub(r"^```(?:yaml)?\s*", "", yaml_output, flags=re.IGNORECASE)
        yaml_output = re.sub(r"```\s*$", "", yaml_output).strip()

        logger.info("   ✅ Successfully extracted cloud-agnostic YAML blueprint.")

        # Diagnostic: persist intermediate YAML next to the source .tf so we can
        # attribute downstream bugs to the correct stage (extraction vs. generation).
        # Without this, an inversion or omission in the final HCL is ambiguous —
        # it could originate here OR in aws_engine.py / azure_engine.py. This file
        # is gitignored (see .gitignore: _intermediate_blueprint_*.yaml).
        # Save failure is non-fatal; the main pipeline must not be blocked by it.
        try:
            base = os.path.basename(source_filename)
            clean = base.replace("google_", "").rsplit(".", 1)[0]
            yaml_path = os.path.join(
                os.path.dirname(source_filename) or ".",
                f"_intermediate_blueprint_{clean}.yaml"
            )
            with open(yaml_path, "w", encoding="utf-8") as fh:
                fh.write(yaml_output)
            logger.info(f"   📝 Intermediate blueprint persisted for diagnosis: {yaml_path}")
        except Exception as save_err:
            logger.warning(f"   ⚠️  Could not persist intermediate YAML (diagnostic only, pipeline continues): {save_err}")

        return yaml_output

    except Exception as e:
        logger.exception(f"   ❌ An error occurred during YAML extraction: {e}")
        return None