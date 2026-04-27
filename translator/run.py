# my-terraform-agent/translator/run.py

import os
from typing import Optional, Tuple

from common.logging import get_logger

from . import config, yaml_engine, aws_engine, azure_engine, tf_validator

# Module-level logger used by the CLI main(). Every call into
# run_translation_pipeline() rebinds tenant context onto a per-call
# logger via _log.bind() -- see the P3-2 docstring section in the
# function for rationale.
_log = get_logger(__name__)


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


def run_translation_pipeline(
    target_cloud: str,
    source_file_path: str,
    *,
    tenant_id: Optional[str] = None,
    project_id: Optional[str] = None,
) -> Tuple[bool, Optional[str]]:
    """Core headless translation pipeline.

    Decoupled from CLI inputs to allow execution via API, UI, or LangGraph.

    Args:
        target_cloud:     "aws" or "azure" (case-insensitive).
        source_file_path: Path to the GCP `.tf` source file.
        tenant_id:        SaaS tenant identifier (Phase 5+). Bound onto
            every log line emitted by this call so Cloud Logging can
            filter per-tenant. None in CLI invocations -- structured
            logs render it as "unknown" so dashboards still parse.
        project_id:       GCP project ID the source file describes.
            Same binding behaviour as tenant_id.

    Returns:
        Tuple[bool, Optional[str]]: (Success boolean, Path to saved
        output file). False / None on any pipeline failure.

    Tenant-context bindings (P3-2)
    ------------------------------
    The Phase 0 audit's translator WARN list flagged that the public
    entry point took paths but no tenant context, so structured logs
    couldn't tag tenant. SaaS log dashboards needed this information
    for per-tenant filtering / alerting.

    Per-call bound logger is preferred over global ``bind_context()``
    because translator pipelines may run concurrently in Cloud Run --
    each call gets its own logger instance with isolated bindings via
    structlog's ``.bind()``. No risk of tenant A's context bleeding
    into tenant B's logs even when the workflows interleave.
    """
    log = _log.bind(
        target_cloud=target_cloud,
        source_file=os.path.basename(source_file_path),
        tenant_id=tenant_id or "unknown",
        project_id=project_id or "unknown",
    )

    target = target_cloud.strip().lower()
    if target not in ['aws', 'azure']:
        log.error("translate_invalid_target_cloud", got=target_cloud)
        return False, None

    if not os.path.isfile(source_file_path):
        log.error("translate_source_file_not_found", source_path=source_file_path)
        return False, None

    try:
        with open(source_file_path, 'r', encoding='utf-8') as f:
            source_hcl = f.read()
    except Exception as e:  # noqa: BLE001 -- caller surfaces failure
        log.exception("translate_source_file_read_failed",
                      source_path=source_file_path, error=str(e))
        return False, None

    log.info("translate_pipeline_start",
             phase="extract_blueprint",
             source_size_bytes=len(source_hcl))

    # 3. Phase 1: Extract Blueprint (Shared logic)
    yaml_blueprint = yaml_engine.extract_yaml_blueprint(source_hcl, source_file_path)
    if not yaml_blueprint:
        log.error("translate_blueprint_extract_failed")
        return False, None

    log.debug("translate_blueprint_preview",
              first_lines=yaml_blueprint.splitlines()[:5],
              total_lines=len(yaml_blueprint.splitlines()))

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
            log.info("translate_phase2_attempt",
                     attempt=attempt, max=max_attempts, mode="initial")
            raw_target_hcl = engine_fn(yaml_blueprint, source_file_path)
        else:
            log.info("translate_phase2_attempt",
                     attempt=attempt, max=max_attempts, mode="retry_with_feedback")
            raw_target_hcl = engine_fn(
                yaml_blueprint,
                source_file_path,
                correction_context={"prev_hcl": prev_hcl, "error": validation_msg},
            )

        if not raw_target_hcl:
            log.error("translate_engine_empty_output", attempt=attempt)
            return False, None

        final_target_hcl = _clean_and_format_hcl(raw_target_hcl)

        # Pillar 1 Proof: Syntactic Validation
        is_valid, validation_msg = tf_validator.validate_hcl(final_target_hcl, target)
        if is_valid:
            if attempt > 1:
                log.info("translate_self_correction_ok",
                         attempt=attempt, max=max_attempts)
            break

        # Validation failed — prepare context for the next retry.
        prev_hcl = final_target_hcl
        if attempt < max_attempts:
            log.warning("translate_validation_retry",
                        attempt=attempt, max=max_attempts,
                        validation_error_first_line=validation_msg.splitlines()[0]
                            if validation_msg else "")
        else:
            log.warning("translate_validation_exhausted",
                        max=max_attempts,
                        validation_error_first_line=validation_msg.splitlines()[0]
                            if validation_msg else "")

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

        log.info("translate_complete",
                 output_path=output_path,
                 validated=is_valid,
                 validation_error=validation_msg if not is_valid else None)

        return is_valid, output_path

    except Exception as e:  # noqa: BLE001
        log.exception("translate_output_write_failed",
                      output_path=output_path, error=str(e))
        return False, None


def main():
    """Interactive CLI wrapper for local testing.

    print() statements remain in this function -- they're interactive UI
    prompts, not operational events. Operational events live in
    run_translation_pipeline() above and now flow through the structured
    logger. Same separation as importer/run.py's CLI vs operational logs.
    """
    print("\n" + "=" * 70)
    print("   MULTI-CLOUD IaC TRANSLATION ENGINE")
    print("=" * 70)

    # 1. Choose Target Cloud
    while True:
        target = input("\nTranslate GCP resource to [AWS] or [Azure]? (Enter 'aws' or 'azure'): ").strip().lower()
        if target in ['aws', 'azure']:
            break
        print("❌ Invalid choice. Please enter 'aws' or 'azure'.")

    # 2. Get Source File
    source_file = input("\nEnter the path to the GCP .tf file you want to translate: ").strip()

    # 3. Execute the headless pipeline.
    # CLI invocations don't have a tenant context (the SaaS UI does -- see
    # Phase 5 packaging where tenant_id flows in from the request envelope).
    # The pipeline accepts None for both and renders structured logs with
    # tenant_id="unknown" / project_id="unknown" so dashboards parse cleanly.
    success, output_file = run_translation_pipeline(target, source_file)

    print("\n" + "=" * 70)
    print(f"TRANSLATION COMPLETE ({target.upper()})")
    print("=" * 70)


if __name__ == "__main__":
    main()
