# policy/scan.py
"""Headless Policy scan entry point for SaaS / programmatic callers.

PUI-5b1 (2026-04-30): introduced as the SaaS-callable counterpart to
``policy/run.py:main()``. Same engine, same defenses, different
contract:

  * ``main()`` is a CLI presentation layer -- argparse, ``os.chdir``,
    interactive prompt, ``print()`` output, returns ``int`` exit code.
    Designed for humans typing commands.
  * ``scan(project_id, *, project_root)`` is a pure programmatic
    function -- no chdir (multi-tenant safe), no interactive input,
    no print side effects, returns a ``PolicyReport`` dataclass.
    Designed for Streamlit pages, tests, future API endpoints.

Why split them: ``os.chdir`` mutates process-global state (race
condition under concurrent Cloud Run requests serving different
projects), and ``input()`` would hang the Streamlit worker thread
forever. Pre-PUI-5b1 the SaaS Detector page couldn't surface Policy
findings without re-implementing main()'s aggregation logic.

Mirrors the same split detector did in P4-3:
  * ``detector/run.py:main()`` -- CLI presentation
  * ``detector/rescan.py:rescan()`` -- programmatic surface

Same applies to importer.run.run_workflow() and
translator.run.run_workflow() -- each engine has BOTH surfaces.

----------------------------------------------------------------------
CLI defenses preserved (PUI-5b1 audit; user-explicit requirement):
----------------------------------------------------------------------

  D1. CONFTEST BINARY CHECK -- ``engine.ensure_conftest_available()``
      raises RuntimeError with install hint if `conftest` not on PATH.
      We let it propagate; the SaaS page catches RuntimeError
      separately and renders "admin must rebuild Docker image" banner.

  D2. PER-RESOURCE 30s TIMEOUT on conftest invocation -- enforced
      inside engine.evaluate() at engine.py:144 (subprocess timeout).
      Untouched by this wrapper.

  D3. PER-RUN VIOLATION CAP (1000 by default) -- enforced HERE in
      the loop below. ``cap_hit`` field on PolicyReport surfaces it
      to the UI which MUST render a truncation banner.

  D4. SUBPROCESS ERROR FAIL-OPEN -- TimeoutExpired / OSError /
      SubprocessError inside engine.evaluate() return [] not raise.
      Untouched by this wrapper.

  D5. JSON DECODE FAIL-OPEN -- bad conftest stdout returns [] not
      raise. Untouched by this wrapper.

  D6. CONFTEST ENGINE-ERROR FAIL-OPEN -- conftest exit code >1
      returns [] + log warning. Untouched by this wrapper.

  D7. MISSING CLOUD SNAPSHOT -> LOW finding -- if cloud_snapshot.fetch
      returns None for a resource (gcloud failed, resource deleted
      out-of-band), we synthesize a LOW Violation with rule_id
      ``cloud_snapshot_missing`` so the report still mentions the
      resource. Same logic as CLI's _scan_resource() helper.

  D8. SNAPSHOT WRITE BEST-EFFORT -- write_snapshot wrapped in
      try/except. A snapshot persistence failure (network, perms,
      env-gate off) MUST NOT take down the engine. Same wrap as CLI.

  D9. IN-SCOPE FILTER -- only scan tf_types in IN_SCOPE_TF_TYPES.
      Out-of-scope state resources never reach the engine.

  D10. POLICY FILE PATH WINDOWS FALLBACK -- engine-side, untouched.

  D11. DETECTOR DECORATION FAIL-OPEN -- if policy module imports
      fail in detector context, drift decoration silently skips.
      Engine-side, untouched.

----------------------------------------------------------------------

Returns ``PolicyReport`` dataclass (see policy/policy_report.py).
"""

from __future__ import annotations

import json
import os
import time
from collections import defaultdict
from typing import Dict, List, Optional

# One-way reuse of the detector's already-built input layer.
# Same imports pattern as policy/run.py -- this module wraps the
# same engine code, just with a different (programmatic) interface.
from detector import config as detector_config
from detector import state_reader, cloud_snapshot

from common.errors import PreflightError
from common.logging import get_logger

from . import config, engine
from .policy_report import PolicyReport

_log = get_logger(__name__)


def _scan_resource(resource, snapshot: Optional[dict]) -> List[engine.Violation]:
    """Evaluate one resource against its applicable policy bundle.

    Common policies (`policies/common/`) apply to every in-scope type;
    per-type policies (`policies/<tf_type>/`) apply only to that type.
    Both directories are passed to conftest in the same call so the
    output is one unified list.

    PUI-5b1 D7 (cloud snapshot missing -> LOW finding): preserved
    verbatim from CLI's _scan_resource at policy/run.py:45-64.
    """
    if snapshot is None:
        # Resource missing from cloud -- no document to evaluate. Surface
        # this as a LOW finding so the report still mentions it but the
        # CI exit code isn't tripped on infra absence (the detector is
        # the right place to gate on missing-from-cloud).
        return [engine.Violation(
            severity="LOW",
            rule_id="cloud_snapshot_missing",
            message="cannot evaluate policies (cloud snapshot unavailable)",
            resource_address=resource.tf_address,
            policy_file="(infrastructure)",
        )]

    dirs_to_check = [
        config.COMMON_POLICY_DIR,
        config.policies_dir_for(resource.tf_type),
    ]
    return engine.evaluate(
        document=snapshot,
        policy_dirs=dirs_to_check,
        resource_address=resource.tf_address,
    )


def scan(project_id: str, *, project_root: str) -> PolicyReport:
    """Headless Policy compliance scan; returns a structured PolicyReport.

    Reads ``<project_root>/<config.STATE_FILE_NAME>``, fetches live
    cloud JSON for each in-scope resource, evaluates against the
    vendored Rego policy bundle, and returns a structured report.

    Args:
        project_id: GCP project to scan. Caller is responsible for
            ADC / SA impersonation setup.
        project_root: Per-project workdir absolute path. Required;
            no silent cwd fallback (matches detector.rescan.rescan
            P4-1 hygiene contract -- multi-tenant Streamlit must
            never fall back to process cwd).

    Returns:
        PolicyReport with per_resource map populated. Empty
        ``per_resource`` is a valid result -- means no in-scope
        resources existed in state. ``cap_hit=True`` means the
        per-run violation cap was reached; UI MUST surface this.

    Raises:
        PreflightError: project_root is missing / unreadable. Same
            stage tag (`resolve_workdir`) as detector.rescan.rescan
            so dashboards filter both with one query.
        RuntimeError: conftest binary is not on PATH. Caller (SaaS
            Streamlit page) should catch separately and render
            "admin must rebuild Docker image" banner with the
            install hint that the engine raised.
        Other exceptions: any unexpected engine failure propagates;
            caller wraps in render_error(). Engine-internal failures
            (subprocess timeout, conftest engine error, JSON decode)
            are FAIL-OPEN inside engine.evaluate() and never bubble
            up here -- they appear as missing violations not
            exceptions.
    """
    # PUI-5b1 D-resolve_workdir: same preflight as detector.rescan.
    if not project_root:
        raise PreflightError(
            "policy.scan() called without project_root; refusing to "
            "fall back to process cwd (would risk wrong-tenant state "
            "reads under concurrency).",
            stage="resolve_workdir",
            reason="missing_project_root_arg",
        )
    if not os.path.isdir(project_root):
        raise PreflightError(
            f"policy.scan() project_root does not exist: {project_root}",
            stage="resolve_workdir",
            reason="project_root_not_a_directory",
        )

    log = _log.bind(project_id=project_id, op="policy_scan")
    log.info("policy_scan_start", project_root=project_root)
    started = time.monotonic()

    # PUI-5b1 D1: fail fast if conftest is missing. Caller catches
    # RuntimeError separately to render an admin-actionable banner.
    engine.ensure_conftest_available()

    # PUI-5b1 D-state-read: read tfstate from per-project workdir.
    # Same path resolution as detector.rescan -- caller (SaaS page)
    # MUST have already materialized the GCS-backend state to the
    # local file via importer.terraform_client.state_pull().
    state_path = os.path.join(project_root, detector_config.STATE_FILE_NAME)
    resources = state_reader.read_state(state_path)
    if not resources:
        log.info("policy_scan_complete_empty_state",
                 reason="no managed resources in state")
        elapsed = time.monotonic() - started
        return PolicyReport(
            project_id=project_id,
            per_resource={},
            n_resources=0,
            compliant_resources=0,
            cap_hit=False,
            duration_s=round(elapsed, 2),
        )

    # PUI-5b1 D9: in-scope filter. Out-of-scope tf_types never reach
    # the engine. Mirrors policy/run.py:167.
    in_scope = [
        r for r in resources if r.tf_type in config.IN_SCOPE_TF_TYPES
    ]
    if not in_scope:
        log.info("policy_scan_complete_no_in_scope",
                 total_state_resources=len(resources),
                 in_scope_types=sorted(config.IN_SCOPE_TF_TYPES))
        elapsed = time.monotonic() - started
        return PolicyReport(
            project_id=project_id,
            per_resource={},
            n_resources=0,
            compliant_resources=0,
            cap_hit=False,
            duration_s=round(elapsed, 2),
        )

    log.info("policy_scan_evaluating",
             in_scope_count=len(in_scope),
             out_of_scope_count=len(resources) - len(in_scope))

    # PUI-5b1 D-snapshot-fetch: parallel cloud snapshot fetch. Already
    # threadpooled at MAX_SNAPSHOT_WORKERS (8). Same call as CLI.
    snapshots = cloud_snapshot.fetch_snapshots(in_scope)

    # PUI-5b1 D3: per-run violation cap. Tracks running total across
    # all resources; once we exceed config.MAX_VIOLATIONS_PER_RUN we
    # stop adding to per_resource and set cap_hit=True. Defends
    # against the malicious-tf case (10k trivial resources => 10k+
    # violations blowing up output / log volume / dashboard rendering
    # costs). Mirrors policy/run.py:177-209.
    per_resource: Dict[str, List[engine.Violation]] = {}
    run_total = 0
    cap_run = config.MAX_VIOLATIONS_PER_RUN
    cap_hit = False

    for r in in_scope:
        if run_total >= cap_run:
            cap_hit = True
            break
        snap = snapshots.get(r.tf_address)
        # PUI-5b1 D7-prep: snapshot may be dict OR raw JSON string
        # depending on cloud_snapshot's internal path. Tolerate both
        # (cloud_snapshot's contract isn't documented as one or the
        # other, so we handle either rather than coupling to an
        # implementation detail). Same shape as CLI policy/run.py:194.
        if isinstance(snap, str):
            try:
                snap = json.loads(snap)
            except (json.JSONDecodeError, TypeError):
                snap = None
        violations = _scan_resource(r, snap)
        # If adding all of this resource's violations would exceed
        # the cap, take only the budget remainder. Rare but matters
        # for determinism (operator should always see exactly cap_run
        # entries, not an off-by-one count).
        remaining = cap_run - run_total
        if len(violations) > remaining:
            violations = violations[:remaining]
            cap_hit = True
        per_resource[r.tf_address] = violations
        run_total += len(violations)

    if cap_hit:
        log.warning(
            "policy_scan_cap_hit",
            cap=cap_run,
            evaluated_count=len(per_resource),
            in_scope_count=len(in_scope),
            reason="per-run violation cap reached; subsequent "
                   "resources/violations not evaluated. Usually "
                   "indicates a buggy rule, malicious input, or "
                   "an unusually large project.",
        )

    elapsed = time.monotonic() - started
    compliant_resources = sum(
        1 for vs in per_resource.values() if not vs
    )
    report = PolicyReport(
        project_id=project_id,
        per_resource=per_resource,
        n_resources=len(per_resource),
        compliant_resources=compliant_resources,
        cap_hit=cap_hit,
        duration_s=round(elapsed, 2),
    )

    log.info("policy_scan_complete", **report.as_fields())

    # PUI-5b1 D8: snapshot persistence is best-effort. A write failure
    # (network, perms, env-gate off) MUST NOT take down the engine.
    # Mirrors policy/run.py:222-254.
    try:
        from common.snapshots import write_snapshot
        write_snapshot("policy", report.as_fields(), project_id)
    except Exception as snap_err:  # noqa: BLE001 -- best-effort
        log.warning(
            "snapshot_write_skipped",
            engine="policy",
            error=str(snap_err),
            reason="snapshot persistence failed; engine result unaffected",
        )

    return report
