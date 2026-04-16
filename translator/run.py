# my-terraform-agent/translator/run.py

import os
import logging
from typing import Optional, Tuple
from . import config, yaml_engine, aws_engine, azure_engine, tf_validator

# Initialize standard logger for enterprise observability
logger = logging.getLogger(__name__)

def _clean_and_format_hcl(raw_hcl: str) -> str:
    """Safety-net function to ensure no trailing markdown artifacts remain."""
    if not raw_hcl: 
        return ""
    clean_lines = [line for line in raw_hcl.splitlines() if not line.strip().startswith("```")]
    return "\n".join(clean_lines)

def run_translation_pipeline(target_cloud: str, source_file_path: str) -> Tuple[bool, Optional[str]]:
    """
    Core headless translation pipeline. 
    Decoupled from CLI inputs to allow execution via API, UI, or LangGraph.
    
    Returns:
        Tuple[bool, Optional[str]]: (Success boolean, Path to saved output file)
    """
    target = target_cloud.strip().lower()
    if target not in ['aws', 'azure']:
        logger.error(f"❌ Invalid target cloud: {target}. Must be 'aws' or 'azure'.")
        return False, None

    if not os.path.isfile(source_file_path):
        logger.error(f"❌ Error: File '{source_file_path}' not found.")
        return False, None

    try:
        with open(source_file_path, 'r', encoding='utf-8') as f:
            source_hcl = f.read()
    except Exception as e:
        logger.exception(f"❌ Error reading source file: {e}")
        return False, None

    # 3. Phase 1: Extract Blueprint (Shared logic)
    yaml_blueprint = yaml_engine.extract_yaml_blueprint(source_hcl, source_file_path)
    if not yaml_blueprint: 
        return False, None
        
    logger.debug("\n   [DEBUG] Extracted Blueprint Preview:")
    for line in yaml_blueprint.splitlines()[:5]: 
        logger.debug(f"   {line}")
    logger.debug("   ...")

    # 4. Phase 2: Generate Target HCL (Routed logic)
    if target == "aws":
        raw_target_hcl = aws_engine.generate_aws_hcl(yaml_blueprint, source_file_path)
        prefix = "aws"
    else:
        raw_target_hcl = azure_engine.generate_azure_hcl(yaml_blueprint, source_file_path)
        prefix = "azure"
    
    if not raw_target_hcl: 
        return False, None
        
    final_target_hcl = _clean_and_format_hcl(raw_target_hcl)

    # 5. Define Output Filename
    base_name = os.path.basename(source_file_path)
    clean_name = base_name.replace("google_", "")
    new_filename = f"{prefix}_translated_{clean_name}"
    output_path = os.path.join(os.path.dirname(source_file_path), new_filename)

    # 6. Pillar 1 Proof: Syntactic Validation
    is_valid, validation_msg = tf_validator.validate_hcl(final_target_hcl, target)

    # 7. Save the Output
    try:
        with open(output_path, "w", encoding='utf-8') as f: 
            f.write(final_target_hcl)
        
        if is_valid:
             logger.info(f"\n🎉 SUCCESS! Validated {target.upper()} code saved to: {output_path}")
        else:
             logger.warning(f"\n⚠️  WARNING: Translated code saved to: {output_path}")
             logger.warning("   The code failed automated syntax validation. It requires manual review.")
             logger.warning(f"   Validation Error Details:\n{validation_msg}")
             
        return is_valid, output_path
             
    except Exception as e:
        logger.exception(f"❌ Error writing translated file: {e}")
        return False, None

def main():
    """Interactive CLI wrapper for local testing."""
    # Configure basic logging for the CLI (In production, this would be set in main.py)
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    
    print("\n" + "="*70)
    print("   MULTI-CLOUD IaC TRANSLATION ENGINE")
    print("="*70)

    # 1. Choose Target Cloud
    while True:
        target = input("\nTranslate GCP resource to [AWS] or [Azure]? (Enter 'aws' or 'azure'): ").strip().lower()
        if target in ['aws', 'azure']:
            break
        print("❌ Invalid choice. Please enter 'aws' or 'azure'.")

    # 2. Get Source File
    source_file = input("\nEnter the path to the GCP .tf file you want to translate: ").strip()
    
    # 3. Execute the headless pipeline
    success, output_file = run_translation_pipeline(target, source_file)

    print("\n" + "="*70)
    print(f"TRANSLATION COMPLETE ({target.upper()})")
    print("="*70)

if __name__ == "__main__":
    main()