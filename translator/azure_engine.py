# my-terraform-agent/translator/azure_engine.py

import re
import logging
from typing import Optional, Dict
from langchain_core.messages import SystemMessage, HumanMessage, AIMessage
from .. import llm_provider
from . import config

# Initialize standard logger for enterprise observability
logger = logging.getLogger(__name__)

def generate_azure_hcl(
    yaml_blueprint: str,
    source_filename: str,
    correction_context: Optional[Dict[str, str]] = None,
) -> Optional[str]:
    """
    Phase 2: Converts the generic YAML blueprint into valid AzureRM HCL code,
    incorporating specific architectural rules and a Traceability Matrix.

    Phase I (validate-feedback loop): if `correction_context` is provided,
    this is a retry call after a prior validation failure. The previous HCL
    output and the validation error are appended to the conversation as
    AIMessage + HumanMessage, prompting the LLM to self-correct without
    losing the original system instruction or blueprint context.

    correction_context shape (when present):
        {"prev_hcl": <previous attempt's HCL>, "error": <validation error string>}
    """
    if correction_context is None:
        logger.info(f"🏗️ [Phase 2] Generating Azure HCL and Traceability Matrix for {source_filename}...")
    else:
        logger.info(f"🔁 [Phase 2 retry] Re-generating Azure HCL with validation-error feedback for {source_filename}...")

    # System prompt: Defines the persona, rules, and expected format.
    system_instruction = (
        "You are an Expert Azure Cloud Architect. Your task is to write production-ready Terraform HCL "
        "for the `hashicorp/azurerm` provider based on the provided generic infrastructure blueprint.\n\n"
        
        f"{config.AZURE_ARCHITECTURAL_RULES}\n\n"
        
        "CRITICAL OUTPUT FORMAT INSTRUCTIONS:\n"
        "Your output must consist of exactly TWO parts, formatted exactly as shown below.\n\n"
        
        "PART 1: THE TRACEABILITY MATRIX (Must be at the very top)\n"
        "You MUST include a multi-line comment block that explains how you mapped the generic concepts "
        "to specific Azure resources. Use this exact format:\n"
        "/*\n"
        "--- MULTI-CLOUD TRANSLATION TRACEABILITY MATRIX ---\n"
        "Blueprint Concept         | Target Azure Resource/Argument | Architectural Justification\n"
        "--------------------------------------------------------------------------------------\n"
        "[Concept 1]               | [azurerm_resource.name]        | [Brief explanation]\n"
        "--------------------------------------------------------------------------------------\n"
        "*/\n\n"
        
        "PART 2: THE TERRAFORM HCL CODE\n"
        "Below the Traceability Matrix, write all required `resource` or `data` blocks.\n"
        "Do NOT include a `provider` or `terraform` block. Output only the resource definitions.\n"
        "Do NOT wrap your entire output in markdown fences (like ```hcl).\n"
    )

    # Human prompt: Provides the actual data payload.
    human_instruction = (
        "--- GENERIC INFRASTRUCTURE BLUEPRINT (YAML) ---\n"
        f"{yaml_blueprint}\n"
        "-----------------------------------------------\n"
    )

    try:
        if correction_context is None:
            logger.info("   - Sending blueprint to Azure Generation Engine (Gemini)...")
        else:
            logger.info("   - Sending blueprint + previous attempt + validation error to Azure Generation Engine (Gemini)...")
        llm_client = llm_provider.get_llm_text_client()

        # Using structured messages (System + Human) for optimal Gemini 2.5 Pro performance
        messages = [
            SystemMessage(content=system_instruction),
            HumanMessage(content=human_instruction)
        ]

        # Phase I: append the prior failed attempt + the validator error so the LLM
        # can self-correct with full context. The LLM is dramatically better at
        # FIXING its own output when shown the error than at AVOIDING the mistake
        # in the first place. This catches long-tail bug classes (cycles, schema
        # drift, novel hallucinations) without requiring new prompt rules.
        if correction_context is not None:
            prev_hcl = correction_context.get("prev_hcl", "")
            error_text = correction_context.get("error", "")
            correction_human = (
                "The HCL you generated above failed `terraform validate` (or one of the\n"
                "fast pre-checks: variable-declaration completeness) with this error:\n\n"
                "----- VALIDATION ERROR -----\n"
                f"{error_text}\n"
                "----------------------------\n\n"
                "Fix the SPECIFIC error reported above. Regenerate the COMPLETE output\n"
                "(Traceability Matrix + HCL), preserving every other resource, comment,\n"
                "variable declaration, and matrix row exactly as before. Do not introduce\n"
                "unrelated changes. Output format is unchanged: matrix block first, then\n"
                "HCL, no markdown fences."
            )
            messages.extend([
                AIMessage(content=prev_hcl),
                HumanMessage(content=correction_human),
            ])

        # P3-5: see yaml_engine.py for safe_invoke rationale.
        response = llm_provider.safe_invoke(llm_client, messages)
        hcl_output = response.content.strip()

        if not hcl_output:
            logger.error("   ❌ Generation failed: LLM returned an empty response.")
            return None

        # Robust cleanup: Remove markdown fences even if they appear in the middle of the text
        hcl_output = re.sub(r"```(?:hcl|terraform)?", "", hcl_output, flags=re.IGNORECASE)
        hcl_output = hcl_output.strip()
        # Robust extraction: Look for code within markdown blocks if present.
        fence_match = re.search(r"```(?:hcl|terraform)?\s*(.*?)\s*```", hcl_output, re.DOTALL | re.IGNORECASE)
        if fence_match:
            hcl_output = fence_match.group(1)

        hcl_output = re.sub(r"```(?:hcl|terraform)?", "", hcl_output, flags=re.IGNORECASE).strip()

        # Basic check to ensure the Traceability Matrix is present
        if "TRACEABILITY MATRIX" not in hcl_output.upper():
            logger.warning("   ⚠️ Warning: The LLM failed to include the required Traceability Matrix.")

        logger.info("   ✅ Successfully generated Azure HCL code.")
        return hcl_output

    except Exception as e:
        logger.exception(f"   ❌ An error occurred during Azure HCL generation: {e}")
        return None