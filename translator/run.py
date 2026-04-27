# my-terraform-agent/translator/run.py

import glob
import os
import time
from typing import List, Optional, Tuple

from common.logging import get_logger

from . import config, yaml_engine, aws_engine, azure_engine, tf_validator
from ..importer.config import TF_TYPE_TO_GCLOUD_INFO
from .results import FileOutcome, TranslationResult

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


# ---------------------------------------------------------------------------
# CC-6 (P3-6): batch translation -- discovery + multi-file pipeline
# ---------------------------------------------------------------------------

# Importer writes files named `<tf_type>_<hcl_name>.tf` into the
# per-project workdir. To split that filename back into (tf_type,
# hcl_name) we can't use a naive regex -- both halves contain
# underscores, so a greedy match would mis-split (e.g.
# `google_compute_instance_poc_vm.tf` would parse as tf_type=
# `google_compute_instance_poc`, hcl_name=`vm`).
#
# Instead we use the importer's TF_TYPE_TO_GCLOUD_INFO as the
# allowlist of valid tf_type prefixes. Files whose prefix matches a
# known tf_type are translatable; files whose prefix doesn't match
# (custom modules, operator-edited artifacts, README backups) are
# silently skipped at discovery time. Sorted longest-first so prefix
# matching picks the most specific tf_type when multiple known types
# share a common prefix (defensive -- doesn't actually happen in the
# current type set, but cheap insurance for future additions).
_KNOWN_TF_TYPE_PREFIXES = sorted(
    TF_TYPE_TO_GCLOUD_INFO.keys(),
    key=len,
    reverse=True,
)


def _parse_imported_filename(filename: str) -> Optional[Tuple[str, str]]:
    """Split `<tf_type>_<hcl_name>.tf` into a (tf_type, hcl_name) pair.

    Returns None when the filename doesn't match the importer's output
    convention OR when the tf_type isn't recognised in the importer's
    map. None signals "skip this file at discovery" -- we only translate
    files whose tf_type the importer explicitly supports.

    Pure function, no I/O. Safe to unit-test directly.
    """
    if not filename.endswith(".tf"):
        return None
    base = filename[:-3]  # strip .tf
    for tf_type in _KNOWN_TF_TYPE_PREFIXES:
        prefix = f"{tf_type}_"
        if base.startswith(prefix):
            hcl_name = base[len(prefix):]
            if hcl_name:  # non-empty hcl_name required
                return (tf_type, hcl_name)
    return None


def _human_friendly_type(tf_type: str) -> str:
    """Convert a Terraform resource type to a customer-facing label.

    google_compute_instance -> "VM"
    google_storage_bucket   -> "Bucket"
    google_kms_crypto_key   -> "KMS Key"
    ...

    Used by the SaaS UI's checkbox grid (Phase 6) so customers see
    "VM · poc-vm" instead of "google_compute_instance · poc_vm".
    Mirrors the punchlist's CC-6 spec (display_label vs. file_path).

    Falls back to the raw tf_type when there's no curated mapping --
    new types added to the importer don't break the translator's
    discovery; they just appear with their raw provider name.
    """
    return _TF_TYPE_DISPLAY_LABELS.get(tf_type, tf_type)


_TF_TYPE_DISPLAY_LABELS = {
    "google_compute_instance": "VM",
    "google_compute_disk": "Disk",
    "google_compute_subnetwork": "Subnet",
    "google_compute_network": "Network",
    "google_compute_firewall": "Firewall",
    "google_compute_address": "Address",
    "google_compute_instance_template": "Instance Template",
    "google_container_cluster": "GKE Cluster",
    "google_container_node_pool": "GKE Node Pool",
    "google_service_account": "Service Account",
    "google_storage_bucket": "Bucket",
    "google_sql_database_instance": "Cloud SQL",
    "google_kms_key_ring": "KMS Key Ring",
    "google_kms_crypto_key": "KMS Key",
    "google_cloud_run_v2_service": "Cloud Run Service",
    "google_pubsub_topic": "Pub/Sub Topic",
    "google_pubsub_subscription": "Pub/Sub Subscription",
}


def discover_translatable_files(workdir: str) -> List[dict]:
    """Walk a per-project importer workdir; return the translatable file list.

    Returned shape (one dict per discovered .tf file):

        {
            "file_path": str,         # absolute path under workdir
            "tf_type":   str,         # "google_compute_instance"
            "hcl_name":  str,         # "poc_vm"
            "display_label": str,     # "VM · poc-vm"  (customer-facing)
        }

    The list is sorted by display_label for stable presentation in the
    Phase 6 UI checkbox grid. Files that don't match the importer's
    naming convention are silently skipped (they're operator-edited
    artifacts, not importer-generated and not safely translatable).

    Used by:
      * Phase 6 Streamlit Translator tab -- renders one checkbox per
        entry (with `display_label` as the visible text and `file_path`
        as the hidden value).
      * Phase 5 Cloud Run API endpoint -- exposes the same list as
        JSON for any HTTP client.
      * CLI smoke testing -- the existing single-file CLI doesn't use
        this; the new `run_translation_batch` does.

    Args:
        workdir: Absolute path to a per-project importer workdir
            (e.g. ``imported/dev-proj-470211/``). Non-existent / empty
            workdirs return an empty list rather than raising; callers
            distinguish "no files to translate" from "bad workdir" via
            ``os.path.isdir(workdir)`` if they need to.

    Returns:
        Sorted list of dicts as described above. Empty list when no
        translatable files are present.
    """
    if not workdir or not os.path.isdir(workdir):
        return []
    out: List[dict] = []
    for path in glob.glob(os.path.join(workdir, "*.tf")):
        base = os.path.basename(path)
        parsed = _parse_imported_filename(base)
        if parsed is None:
            # Skip files whose tf_type isn't in the importer's known
            # set (operator-edited artifacts, custom modules, custom
            # backends, etc.). The importer's output is the only
            # thing we know how to translate safely; arbitrary HCL
            # would just break the LLM with novel input shapes.
            continue
        tf_type, hcl_name = parsed
        # Re-humanise the hcl_name for display (importer underscored
        # hyphens to make HCL labels valid; the customer originally
        # named the resource with hyphens).
        display_name = hcl_name.replace("_", "-")
        out.append({
            "file_path": path,
            "tf_type": tf_type,
            "hcl_name": hcl_name,
            "display_label": f"{_human_friendly_type(tf_type)} · {display_name}",
        })
    # Stable order for UI rendering.
    out.sort(key=lambda e: e["display_label"])
    return out


def run_translation_batch(
    target_cloud: str,
    source_paths: List[str],
    *,
    tenant_id: Optional[str] = None,
    project_id: Optional[str] = None,
) -> TranslationResult:
    """Translate multiple source files to the same target cloud.

    Phase 6 Streamlit Translator tab calls this with the operator's
    checkbox selection (a list of file_paths from
    `discover_translatable_files`). Per-file failures land in the
    returned TranslationResult.files list with status="failed" /
    "needs_attention"; the batch as a whole completes regardless.

    Same A+D contract as importer.run_workflow:
      * RAISES on inputs/environment problems (invalid target_cloud,
        empty source_paths). Caller catches; UI renders the
        PreflightError's user_hint.
      * RETURNS a TranslationResult on every successful batch run,
        regardless of per-file outcomes.

    Per-file failure isolation: each file's translation is wrapped
    in try/except so one file's LLM error / validator failure / write
    error doesn't kill the batch (mirror of C5.1 Bug B fix in the
    importer).

    Args:
        target_cloud: "aws" or "azure" (case-insensitive). Same value
            applies to every file in the batch.
        source_paths: List of source ``.tf`` paths to translate.
            Empty list raises ValueError -- empty batches are a caller
            bug, not a runtime case to handle silently.
        tenant_id / project_id: Same SaaS context plumbing as
            run_translation_pipeline. Bound onto the batch logger so
            every per-file event carries tenant context.

    Returns:
        TranslationResult with per-file outcomes + counts + duration.

    Raises:
        ValueError: empty source_paths or invalid target_cloud.
            Both are caller bugs that warrant fast-failing rather
            than producing a misleading "batch translated 0 of 0
            files" result.
    """
    target = target_cloud.strip().lower()
    if target not in ("aws", "azure"):
        raise ValueError(
            f"target_cloud must be 'aws' or 'azure'; got {target_cloud!r}"
        )
    if not source_paths:
        raise ValueError(
            "run_translation_batch requires a non-empty source_paths list"
        )

    log = _log.bind(
        target_cloud=target,
        tenant_id=tenant_id or "unknown",
        project_id=project_id or "unknown",
        batch_size=len(source_paths),
    )
    log.info("translation_batch_start")

    started = time.monotonic()
    files: List[FileOutcome] = []
    translated = 0
    needs_attention = 0
    failed = 0

    for source_path in source_paths:
        file_started = time.monotonic()
        try:
            is_valid, output_path = run_translation_pipeline(
                target,
                source_path,
                tenant_id=tenant_id,
                project_id=project_id,
            )
        except Exception as e:  # noqa: BLE001 -- isolate per-file failures
            # Per-file failure -- one bad file does NOT kill the batch.
            # This is the C5.1 Bug B equivalent for the translator: a
            # single LLM exception or write error in file N must not
            # propagate up and crash files N+1..len(source_paths).
            file_duration = round(time.monotonic() - file_started, 2)
            log.error(
                "translation_file_outcome",
                source_path=source_path,
                status="failed",
                error_type=type(e).__name__,
                error=str(e),
                duration_s=file_duration,
            )
            files.append(FileOutcome(
                source_path=source_path,
                target_cloud=target,
                status="failed",
                output_path=None,
                validation_error=f"{type(e).__name__}: {e}",
                duration_s=file_duration,
            ))
            failed += 1
            continue

        file_duration = round(time.monotonic() - file_started, 2)
        if output_path is None:
            # Pipeline returned (False, None) -- couldn't even produce
            # output (LLM empty, file read error, etc.). Counted as
            # `failed`, not `needs_attention`.
            log.warning(
                "translation_file_outcome",
                source_path=source_path,
                status="failed",
                duration_s=file_duration,
            )
            files.append(FileOutcome(
                source_path=source_path,
                target_cloud=target,
                status="failed",
                output_path=None,
                validation_error="pipeline returned no output path",
                duration_s=file_duration,
            ))
            failed += 1
        elif is_valid:
            log.info(
                "translation_file_outcome",
                source_path=source_path,
                status="translated",
                output_path=output_path,
                duration_s=file_duration,
            )
            files.append(FileOutcome(
                source_path=source_path,
                target_cloud=target,
                status="translated",
                output_path=output_path,
                validation_error="",
                duration_s=file_duration,
            ))
            translated += 1
        else:
            # Output produced but failed validation -- the existing
            # pipeline saves best-effort HCL anyway. UI renders this
            # as "needs attention" per CC-5.
            log.warning(
                "translation_file_outcome",
                source_path=source_path,
                status="needs_attention",
                output_path=output_path,
                duration_s=file_duration,
            )
            files.append(FileOutcome(
                source_path=source_path,
                target_cloud=target,
                status="needs_attention",
                output_path=output_path,
                validation_error="output failed schema validation; saved for review",
                duration_s=file_duration,
            ))
            needs_attention += 1

    duration_s = round(time.monotonic() - started, 2)
    skipped = max(
        0, len(source_paths) - translated - needs_attention - failed,
    )
    result = TranslationResult(
        target_cloud=target,
        selected=len(source_paths),
        translated=translated,
        needs_attention=needs_attention,
        failed=failed,
        skipped=skipped,
        duration_s=duration_s,
        files=files,
    )
    log.info("translation_batch_complete", **result.as_fields())
    return result


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
