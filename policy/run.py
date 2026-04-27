# policy/run.py
"""
On-demand CLI for policy compliance scanning (POC).

Usage:
    python -m policy.run [--project <project_id>]

Reads <workdir>/terraform.tfstate, fetches live cloud JSON for the in-scope
resources, evaluates each against the vendored Rego policy bundle, and
prints a compliance report grouped by severity. Exits non-zero if any
HIGH violation is found (CI gate).

Reuses detector.state_reader and detector.cloud_snapshot so we don't
re-implement state parsing or gcloud invocation. The two modules stay
independent — this is one-way reuse, not a circular dependency.

Per-project workdir refactor
----------------------------
Same project-resolution semantics as ``detector/run.py`` -- see that
module's docstring for the full picker logic. Mirrors the importer's
per-project layout so a CI job runs::

    python -m policy.run --project prod-470211

against ``imported/prod-470211/terraform.tfstate``, never touching the
sibling dev project's state.
"""

import argparse
import json
import os
import sys
from collections import defaultdict
from typing import Dict, List, Optional

# One-way reuse of the detector's already-built input layer.
from detector import config as detector_config
from detector import state_reader, cloud_snapshot
from detector.run import _select_project  # workdir picker (CLI parity)
from common.workdir import resolve_project_workdir

from . import config, engine


def _scan_resource(resource, snapshot: Optional[dict]) -> List[engine.Violation]:
    """Evaluate one resource against its applicable policy bundle.

    Common policies (`policies/common/`) apply to every in-scope type;
    per-type policies (`policies/<tf_type>/`) apply only to that type.
    Both directories are passed to conftest in the same call so the
    output is one unified list.
    """
    if snapshot is None:
        # Resource missing from cloud — no document to evaluate. Surface
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


def _print_report(per_resource: Dict[str, List[engine.Violation]]) -> int:
    """Pretty-print and return count of HIGH-severity violations."""
    print("\n" + "=" * 60)
    print("POLICY COMPLIANCE REPORT")
    print("=" * 60)

    high_count = 0
    compliant_resources = 0
    addrs = sorted(per_resource.keys())

    for addr in addrs:
        violations = per_resource[addr]
        if not violations:
            print(f"\u2705 {addr}  \u2014 all policies passing")
            compliant_resources += 1
            continue
        # Sort: HIGH first, then MED, then LOW.
        violations.sort(key=lambda v: -v.severity_weight)
        print(f"\n\U0001f6d1 {addr}")
        for v in violations:
            print(f"   [{v.severity:<4}] {v.rule_id}  \u2014 {v.message}")
            try:
                pretty_path = os.path.relpath(v.policy_file)
            except ValueError:
                # Cross-drive on Windows; fall back to absolute path.
                pretty_path = v.policy_file
            print(f"           policy: {pretty_path}")
            if v.severity == "HIGH":
                high_count += 1

    print("\n" + "-" * 60)
    severity_totals: Dict[str, int] = defaultdict(int)
    for vs in per_resource.values():
        for v in vs:
            severity_totals[v.severity] += 1
    parts = []
    for sev in ("HIGH", "MED", "LOW"):
        n = severity_totals.get(sev, 0)
        if n:
            parts.append(f"{n} {sev}")
    summary = ", ".join(parts) if parts else "no violations"
    n_resources = len(per_resource)
    n_violating = n_resources - compliant_resources
    print(
        f"Summary: {summary} across {n_violating} resource(s) "
        f"({compliant_resources} of {n_resources} compliant)"
    )
    print("=" * 60)
    return high_count


def main() -> int:
    parser = argparse.ArgumentParser(prog="policy.run")
    parser.add_argument(
        "--project", "-p",
        help="GCP project_id whose per-project workdir to scan. "
             "If omitted, lists available workdirs and prompts.",
    )
    args = parser.parse_args()

    project_id = _select_project(args.project)
    try:
        workdir = resolve_project_workdir(project_id, create=False)
    except ValueError as e:
        print(f"[FAIL] {e}")
        return 2

    if not os.path.isdir(workdir):
        print(f"[FAIL] Workdir does not exist: {workdir}")
        print(f"       Run the importer for project {project_id!r} first.")
        return 2

    os.chdir(workdir)
    print(f"--- Project: {project_id}")
    print(f"--- Working directory: {os.getcwd()} ---")

    # Fail fast if conftest is missing — the standalone CLI needs the
    # engine to do its job. Detector decoration fails open instead.
    try:
        engine.ensure_conftest_available()
    except RuntimeError as e:
        print(f"\n\u274c {e}")
        return 2

    state_path = os.path.join(workdir, detector_config.STATE_FILE_NAME)
    resources = state_reader.read_state(state_path)
    if not resources:
        print("Nothing to scan (no managed resources in state).")
        return 0

    in_scope = [r for r in resources if r.tf_type in config.IN_SCOPE_TF_TYPES]
    if not in_scope:
        print("No in-scope resources for the policy enforcer.")
        return 0

    print(f"\n\U0001f50e Scanning {len(in_scope)} resource(s) "
          f"against vendored Rego policies...")

    snapshots = cloud_snapshot.fetch_snapshots(in_scope)

    # P4-1 per-run cap: track running total of violations across all
    # resources. Once we exceed config.MAX_VIOLATIONS_PER_RUN, stop
    # adding to per_resource and emit a single warning. Defends against
    # the malicious-tf case (10k trivial resources => 10k+ violations
    # blowing up output / log volume / dashboard rendering costs).
    per_resource: Dict[str, List[engine.Violation]] = {}
    run_total = 0
    cap_run = config.MAX_VIOLATIONS_PER_RUN
    cap_hit = False
    for r in in_scope:
        if run_total >= cap_run:
            cap_hit = True
            break
        snap = snapshots.get(r.tf_address)
        # Accept either dict (parsed) or string (raw JSON) — cloud_snapshot's
        # contract isn't documented as one or the other in the detector path,
        # so we tolerate both rather than couple to an implementation detail.
        if isinstance(snap, str):
            try:
                snap = json.loads(snap)
            except (json.JSONDecodeError, TypeError):
                snap = None
        violations = _scan_resource(r, snap)
        # If adding all of this resource's violations would exceed the
        # cap, take only the budget remainder. Rare but matters for
        # determinism (operator should always see exactly cap_run
        # entries, not an off-by-one count).
        remaining = cap_run - run_total
        if len(violations) > remaining:
            violations = violations[:remaining]
            cap_hit = True
        per_resource[r.tf_address] = violations
        run_total += len(violations)

    if cap_hit:
        print(f"\n⚠️  Truncated at {cap_run} violations across the run "
              f"(per-run cap from policy/config.MAX_VIOLATIONS_PER_RUN). "
              f"Subsequent resources / violations were not evaluated. "
              f"This usually indicates a buggy rule, malicious input, "
              f"or an unusually large project -- please review.")

    high_count = _print_report(per_resource)
    return 1 if high_count > 0 else 0


if __name__ == "__main__":
    sys.exit(main())
