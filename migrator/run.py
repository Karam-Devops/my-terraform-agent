"""Migrator engine — public entry point.

A+D contract (parity with importer.run.run_workflow,
translator.run.run_translation_batch, detector.rescan.rescan,
policy.scan.scan):

  * RAISES PreflightError on input/environment failures (bad
    repo_path, target cloud not allowed, hcl2 not installed).
  * RETURNS MigrationResult on every completed run, regardless of
    per-resource outcomes. Per-file parse failures + per-resource
    confidence-low scores are recorded inside the result.

Streamlit UI calls run_migration() directly; CLI is a thin wrapper.
"""

from __future__ import annotations

import os
import time
from typing import Optional

from common.errors import PreflightError
from common.logging import get_logger

from . import config
from .ingest.hcl_parser import is_hcl_parser_available
from .ingest.inventory import build_inventory
from .ingest.repo_walker import walk_repo
from .output.executive_summary import emit_executive_summary
from .output.helpers import emit_helper_scripts
from .output.migration_guide import emit_migration_guide
from .output.terragrunt_emitter import emit_terragrunt_skeleton
from .validate import validate_target as _validate_target
from .plan.coverage import score_resources
from .plan.dep_graph import build_dep_graph
from .results import MigrationResult


_log = get_logger(__name__)


def run_migration(
    repo_path: str,
    *,
    target_cloud: str = "aws",
    output_dir: Optional[str] = None,
    project_id: Optional[str] = None,
    tenant_id: Optional[str] = None,
) -> MigrationResult:
    """End-to-end migrator run.

    Args:
        repo_path: absolute path to a checked-out IaC repo (or a local
            test fixture). Must exist; PreflightError otherwise.
        target_cloud: "aws" today. Validated against
            ``config.MIGRATOR_TARGETS_ALLOWED``.
        output_dir: where MIGRATION_GUIDE.md, helper scripts, and
            (future) the AWS Terragrunt skeleton are written. Defaults
            to ``<repo_path>/migrator_output/`` if not given. Created
            if missing.
        project_id, tenant_id: SaaS context plumbing for structured
            logging. Both default to "unknown".

    Returns:
        MigrationResult with all phases populated. Errors are
        accumulated, not raised — caller renders ``result.errors``
        in the UI banner.

    Raises:
        PreflightError: bad repo_path, disallowed target, hcl2 not
            installed. Streamlit page catches and renders ``.user_hint``.
    """
    log = _log.bind(
        repo_path=repo_path,
        target_cloud=target_cloud,
        project_id=project_id or "unknown",
        tenant_id=tenant_id or "unknown",
    )
    log.info("migrator_start")
    started = time.monotonic()

    # ---- preflight ----
    if not repo_path:
        raise PreflightError(
            "run_migration() called without repo_path",
            stage="validate_repo_path",
            reason="missing_repo_path",
        )
    if not os.path.isdir(repo_path):
        raise PreflightError(
            f"repo_path does not exist or is not a directory: {repo_path}",
            stage="validate_repo_path",
            reason="repo_path_not_found",
        )
    if not config.is_target_allowed(target_cloud):
        raise PreflightError(
            f"target_cloud '{target_cloud}' is not in allowlist "
            f"{config.MIGRATOR_TARGETS_ALLOWED}",
            stage="validate_target_cloud",
            reason="target_cloud_not_allowed",
        )
    if not is_hcl_parser_available():
        raise PreflightError(
            "python-hcl2 is not installed; install with `pip install python-hcl2`",
            stage="validate_dependencies",
            reason="hcl_parser_missing",
        )

    target = target_cloud.strip().lower()
    output_dir = output_dir or os.path.join(repo_path, config.MIGRATOR_OUTPUT_DIRNAME)
    os.makedirs(output_dir, exist_ok=True)

    # ---- discover ----
    walk = walk_repo(repo_path)
    log.info(
        "migrator_walk_complete",
        source_iac=walk.source_iac,
        tf_files=len(walk.tf_files),
        terragrunt_files=len(walk.terragrunt_files),
        tfvars_files=len(walk.tfvars_files),
    )

    resources, ingest_errors = build_inventory(walk)
    log.info(
        "migrator_inventory_built",
        resource_count=len(resources),
        ingest_error_count=len(ingest_errors),
    )

    # ---- plan ----
    dep_edges = build_dep_graph(resources)
    log.info("migrator_depgraph_built", edge_count=len(dep_edges))

    confidence = score_resources(resources, target_cloud=target)
    log.info(
        "migrator_scoring_complete",
        confidence_summary={
            "HIGH":   sum(1 for c in confidence if c.band == "HIGH"),
            "MEDIUM": sum(1 for c in confidence if c.band == "MEDIUM"),
            "LOW":    sum(1 for c in confidence if c.band == "LOW"),
            "MANUAL": sum(1 for c in confidence if c.band == "MANUAL_REVIEW"),
        },
    )

    # ---- generate ----
    guide_path = emit_migration_guide(
        output_dir=output_dir,
        repo_path=repo_path,
        target_cloud=target,
        source_iac=walk.source_iac,
        resources=resources,
        confidence=confidence,
        dep_edges=dep_edges,
    )
    log.info("migrator_guide_emitted", path=guide_path)

    helper_paths = emit_helper_scripts(
        output_dir=output_dir,
        target_cloud=target,
        confidence=confidence,
    )
    log.info("migrator_helpers_emitted", count=len(helper_paths))

    skeleton_paths = emit_terragrunt_skeleton(
        output_dir=output_dir,
        repo_path=repo_path,
        target_cloud=target,
        resources=resources,
        confidence=confidence,
    )
    log.info("migrator_skeleton_emitted", count=len(skeleton_paths))

    # Executive summary — one-page customer take-home
    from .translate import TRANSLATORS as _TRANSLATORS
    _covered = set(_TRANSLATORS.keys())
    _translated_count = sum(1 for r in resources if r.tf_type in _covered)
    exec_summary_path = emit_executive_summary(
        output_dir=output_dir,
        repo_path=repo_path,
        target_cloud=target,
        source_iac=walk.source_iac,
        resources=resources,
        confidence=confidence,
        duration_s=time.monotonic() - started,
        files_scanned=walk.total_files,
        translators_registered=len(set(_TRANSLATORS.values())),  # de-duped
        translated_count=_translated_count,
    )
    log.info("migrator_exec_summary_emitted", path=exec_summary_path)

    # ---- validate (Tiers 0–3, no cloud creds needed) ----
    target_dir = os.path.join(output_dir, "target")
    validation_report = _validate_target(target_dir)
    validation_dict = validation_report.summary
    log.info(
        "migrator_validation_complete",
        overall_passed=validation_dict.get("overall_passed"),
        tiers=validation_dict.get("tiers"),
    )

    duration = round(time.monotonic() - started, 2)
    result = MigrationResult(
        project_id=project_id,
        repo_path=os.path.abspath(repo_path),
        target_cloud=target,
        source_iac=walk.source_iac,
        resources=resources,
        files_scanned=walk.total_files,
        dep_edges=dep_edges,
        confidence=confidence,
        output_dir=output_dir,
        migration_guide_path=guide_path,
        helper_script_paths=helper_paths,
        skeleton_paths=skeleton_paths,
        validation=validation_dict,
        duration_s=duration,
        errors=ingest_errors,
    )

    log.info("migrator_complete", **result.as_fields())

    # Best-effort snapshot persistence (mirrors detector + policy
    # patterns). Never blocks engine completion.
    try:
        from common.snapshots import write_snapshot
        write_snapshot("migrator", result.as_fields(), project_id or "unknown",
                       tenant_id=tenant_id)
    except Exception as snap_err:  # noqa: BLE001 -- best-effort
        log.warning(
            "snapshot_write_skipped", engine="migrator",
            error=str(snap_err),
            reason="snapshot persistence failed; engine result unaffected",
        )

    return result
