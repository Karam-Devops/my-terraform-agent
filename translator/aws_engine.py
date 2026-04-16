# my-terraform-agent/translator/aws_engine.py

import re
import logging
from typing import Optional
from langchain_core.messages import SystemMessage, HumanMessage
from .. import llm_provider
from . import config

# Initialize standard logger for enterprise observability
logger = logging.getLogger(__name__)

def generate_aws_hcl(yaml_blueprint: str, source_filename: str) -> Optional[str]:
    """
    Phase 2: Converts the generic YAML blueprint into valid AWS HCL code,
    incorporating specific architectural rules and a Traceability Matrix.
    """
    logger.info(f"🏗️ [Phase 2] Generating AWS HCL and Traceability Matrix for {source_filename}...")

    # System prompt: Defines the persona, rules, and expected format.
    system_instruction = (
        "You are an Expert AWS Cloud Architect. Your task is to write production-ready Terraform HCL "
        "for the AWS provider based on the provided generic infrastructure blueprint.\n\n"
        
        f"{config.AWS_ARCHITECTURAL_RULES}\n\n"
        
        "CRITICAL OUTPUT FORMAT INSTRUCTIONS:\n"
        "Your output must consist of exactly TWO parts, formatted exactly as shown below.\n\n"
        
        "PART 1: THE TRACEABILITY MATRIX (Must be at the very top)\n"
        "You MUST include a multi-line comment block that explains how you mapped the generic concepts "
        "to specific AWS resources. Use this exact format:\n"
        "/*\n"
        "--- MULTI-CLOUD TRANSLATION TRACEABILITY MATRIX ---\n"
        "Blueprint Concept         | Target AWS Resource/Argument | Architectural Justification\n"
        "--------------------------------------------------------------------------------------\n"
        "[Concept 1]               | [aws_resource.name]          | [Brief explanation]\n"
        "[Concept 2]               | [aws_resource.argument]      | [Brief explanation]\n"
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
        logger.info("   - Sending blueprint to AWS Generation Engine (Gemini)...")
        llm_client = llm_provider.get_llm_text_client()
        
        # Using structured messages (System + Human) for optimal Gemini 2.5 Pro performance
        messages = [
            SystemMessage(content=system_instruction),
            HumanMessage(content=human_instruction)
        ]
        
        response = llm_client.invoke(messages)
        aws_hcl_output = response.content.strip()

        if not aws_hcl_output:
            logger.error("   ❌ Generation failed: LLM returned an empty response.")
            return None

        # Robust cleanup: Remove markdown fences even if they appear in the middle of the text
        # (e.g., if the LLM puts the Matrix outside the fence, but the code inside it)
        aws_hcl_output = re.sub(r"```(?:hcl|terraform)?", "", aws_hcl_output, flags=re.IGNORECASE)
        aws_hcl_output = aws_hcl_output.strip()

        # Basic check to ensure the Traceability Matrix is present
        if "TRACEABILITY MATRIX" not in aws_hcl_output.upper():
            logger.warning("   ⚠️ Warning: The LLM failed to include the required Traceability Matrix.")

        logger.info("   ✅ Successfully generated AWS HCL code.")
        return aws_hcl_output

    except Exception as e:
        logger.exception(f"   ❌ An error occurred during AWS HCL generation: {e}")
        return None