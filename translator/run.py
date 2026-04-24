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


def resolve_output_path(source_file_path: str, target: str, prefix: str) -> str:
    """Compute where a translated `.tf` file should land on disk.

    Old behaviour wrote the translated file next to the source — e.g.
    ``generated_iac/aws_translated_compute_instance.tf`` sat in the same
    directory as ``generated_iac/google_compute_instance.tf``. That made
    the directory increasingly hard to read as more types and target
    clouds got translated; the GCP originals and translated outputs got
    jumbled and `terraform validate` could even pick them up as a single
    workspace.

    New layout writes into a per-target subdirectory of the source dir:

        generated_iac/google_compute_instance.tf            (source)
        generated_iac/translated/aws/aws_translated_compute_instance.tf
        generated_iac/translated/azure/azure_translated_compute_instance.tf

    Pulled out as a pure helper so it can be unit-tested without
    mocking the engine, validator, and LLM. The caller is responsible
    for `os.makedirs(..., exist_ok=True)` before writing — the helper
    does NOT touch the filesystem.

    Args:
        source_file_path: Path to the GCP `.tf` source file.
        target:           Target cloud, lowercased ("aws" or "azure").
        prefix:           Filename prefix for the translated file
                          (typically same as `target`).

    Returns:
        Absolute or relative path (mirrors the input convention) to
        where the translated file should be written.
    """
    base_name = os.path.basename(source_file_path)
    clean_name = base_name.replace("google_", "")
    new_filename = f"{prefix}_translated_{clean_name}"
    # `or "."` guards the bare-basename case (no directory component).
    source_dir = os.path.dirname(source_file_path) or "."
    out_dir = os.path.join(source_dir, "translated", target)
    return os.path.join(out_dir, new_filename)

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

    # 4. Phase 2: Generate Target HCL (Routed logic) with Phase I validate-feedback
    # retry loop. The LLM is dramatically better at FIXING its own output when shown
    # the validator error than at AVOIDING the mistake from prompt rules alone. This
    # bounded loop catches long-tail bug classes (resource cycles, novel argument
    # hallucinations, schema drift) without requiring a new prompt rule per bug.
    if target == "aws":
        engine_fn = aws_engine.generate_aws_hcl
        prefix = "aws"
    else:
        engine_fn = azure_engine.generate_azure_hcl
        prefix = "azure"

    final_target_hcl: Optional[str] = None
    is_valid: bool = False
    validation_msg: str = ""
    prev_hcl: str = ""

    max_attempts = max(1, int(getattr(config, "MAX_RETRIES", 3)))
    for attempt in range(1, max_attempts + 1):
        if attempt == 1:
            logger.info(f"   - Phase 2 attempt {attempt}/{max_attempts} (initial generation)")
            raw_target_hcl = engine_fn(yaml_blueprint, source_file_path)
        else:
            logger.info(f"   - Phase 2 attempt {attempt}/{max_attempts} (retry with validation-error feedback)")
            raw_target_hcl = engine_fn(
                yaml_blueprint,
                source_file_path,
                correction_context={"prev_hcl": prev_hcl, "error": validation_msg},
            )

        if not raw_target_hcl:
            logger.error(f"   ❌ Engine returned empty output on attempt {attempt}.")
            return False, None

        final_target_hcl = _clean_and_format_hcl(raw_target_hcl)

        # Pillar 1 Proof: Syntactic Validation
        is_valid, validation_msg = tf_validator.validate_hcl(final_target_hcl, target)
        if is_valid:
            if attempt > 1:
                logger.info(f"   ✅ Self-correction succeeded on attempt {attempt}/{max_attempts}.")
            break

        # Validation failed — prepare context for the next retry.
        prev_hcl = final_target_hcl
        if attempt < max_attempts:
            logger.warning(f"   🔁 Validation failed on attempt {attempt}/{max_attempts}; feeding error back to LLM and retrying.")
        else:
            logger.warning(f"   ⚠️  Validation still failing after {max_attempts} attempts; saving best-effort output for manual review.")

    # 5. Define Output Filename
    # Per TODO #13: translated files now land in `<source_dir>/translated/<target>/`
    # so they don't get jumbled with the GCP originals. See
    # `resolve_output_path` for the rationale and full layout.
    output_path = resolve_output_path(source_file_path, target, prefix)
    os.makedirs(os.path.dirname(output_path), exist_ok=True)

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