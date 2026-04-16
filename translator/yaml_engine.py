# my-terraform-agent/translator/yaml_engine.py

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
        "1. **Strip Provider Syntax:** Do NOT include any provider-specific names (like 'google_compute_instance' or 'aws_instance'). Use generic terms like 'virtual_machine', 'database', or 'object_storage'.\n"
        "2. **Abstract Values:** Convert specific machine types or zones into descriptive concepts. For example, instead of 'e2-medium', use 'size: medium_general_purpose'. Instead of 'us-central1-a', use 'location: us_central_zone_1'.\n"
        "3. **Capture Intent, Not Code:** Focus on the business requirements (e.g., 'needs public IP', 'attached to default network', 'has read-only storage access').\n"
        "4. **Format:** Output ONLY valid YAML. Do not include markdown fences (like ```yaml), comments, or explanations.\n"
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
        return yaml_output

    except Exception as e:
        logger.exception(f"   ❌ An error occurred during YAML extraction: {e}")
        return None