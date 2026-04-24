# detector/run.py
"""
On-demand CLI for drift detection (POC).

Usage:
    python -m detector.run [--project <project_id>]

Reads <workdir>/terraform.tfstate, fetches live cloud JSON for the in-scope
resources, prints a structured drift report, and exits non-zero if drift was
found.

Per-project workdir refactor
----------------------------
The detector now operates on a SINGLE per-project workdir at a time
(``imported/<project_id>/``) -- not the old commingled repo-root state.
The workdir is chosen via:

    1. ``--project <project_id>`` flag (preferred for scripts / CI)
    2. Single workdir present  -> auto-select it
    3. Multiple workdirs       -> interactive picker over
                                  ``common.workdir.list_project_workdirs()``
    4. Zero workdirs           -> exit with a clear "import something first"
                                  message

The CLI ``os.chdir(workdir)`` once at entry so the rest of the module
(remediator's ``_state_path()``, ``_run_terraform()`` subprocess cwd) can
keep using the process cwd. Programmatic callers (Streamlit / FastAPI)
should NOT use this module -- they should drive
``remediator.remediate_one(workdir=...)`` directly, which is thread-safe.
"""

import argparse
import os
import sys

from . import config, state_reader, cloud_snapshot, diff_engine, remediator
from common.workdir import resolve_project_workdir, list_project_workdirs

# Policy enforcer decoration. Imported lazily-by-try so the detector still
# runs cleanly when the policy module isn't present (e.g. early-checkout
# branches) or when conftest isn't installed. Both paths are handled by
# integration.classify_drift returning an empty PolicyImpact.
try:
    from policy import integration as policy_integration
    _POLICY_AVAILABLE = True
except ImportError:
    _POLICY_AVAILABLE = False


def _select_project(explicit: str = None) -> str:
    """Pick which project workdir the detector should run against.

    See module docstring for the resolution order. Returns the project_id
    string (caller resolves it to an absolute workdir path); raises
    SystemExit on no-workdir / user-cancel so the CLI fails cleanly.
    """
    if explicit:
        return explicit

    available = list_project_workdirs()
    if not available:
        print("[FAIL] No per-project workdirs found under imported/.")
        print("       Run the importer first to materialise at least one project.")
        raise SystemExit(2)

    if len(available) == 1:
        only = available[0]
        print(f"--- Single project found: {only} (auto-selected)")
        return only

    print("\nMultiple per-project workdirs found:")
    for i, p in enumerate(available, start=1):
        print(f"  [{i}] {p}")
    while True:
        try:
            raw = input(f"\nSelect project [1-{len(available)}], or 0 to cancel: ").strip()
        except EOFError:
            print("[FAIL] No project selection (stdin closed).")
            raise SystemExit(2)
        if raw == "0":
            print("Cancelled.")
            raise SystemExit(0)
        try:
            idx = int(raw)
            if 1 <= idx <= len(available):
                return available[idx - 1]
        except ValueError:
            pass
        print(f"Invalid input. Enter a number from 1 to {len(available)}.")


def main() -> int:
    parser = argparse.ArgumentParser(prog="detector.run")
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

    # Single chdir so downstream remediator helpers can keep relying on
    # process cwd. Programmatic callers shouldn't use this CLI -- they
    # should call remediator.remediate_one(workdir=...) directly.
    os.chdir(workdir)
    print(f"--- Project: {project_id}")
    print(f"--- Working directory: {os.getcwd()} ---")

    state_path = os.path.join(workdir, config.STATE_FILE_NAME)
    resources = state_reader.read_state(state_path)
    if not resources:
        print("Nothing to do (no managed resources in state).")
        return 0

    state_reader.summarize(resources)

    snapshots = cloud_snapshot.fetch_snapshots(resources)

    drifts = []
    for r in resources:
        if not r.in_scope:
            continue
        drift = diff_engine.diff_resource(
            tf_address=r.tf_address,
            tf_type=r.tf_type,
            state_attrs=r.attributes,
            cloud_json=snapshots.get(r.tf_address),
        )
        # Decorate drifted resources with policy impact. Skip when nothing
        # drifted (clean resources don't need the noise) or when the
        # policy module isn't loadable (fail-open). classify_drift itself
        # is fail-open too -- missing conftest, missing snapshot, or
        # out-of-scope tf_type all return an empty impact.
        if _POLICY_AVAILABLE and drift.has_drift and not drift.error:
            impact = policy_integration.classify_drift(
                tf_address=drift.tf_address,
                tf_type=drift.tf_type,
                cloud_snapshot=snapshots.get(r.tf_address),
            )
            drift.policy_tag = impact.summary_tag
        drifts.append(drift)

    drift_count = diff_engine.print_report(drifts)

    # If we found drift AND we're sitting at an interactive terminal, walk
    # the user through remediation. The remediator no-ops in CI / non-tty
    # contexts so the exit code below remains the canonical CI signal.
    # workdir is passed through so programmatic callers (and future SaaS
    # invocations) don't depend on the chdir we did above.
    if drift_count > 0:
        remediator.run_remediation(drifts, workdir=workdir)

    return 1 if drift_count > 0 else 0


if __name__ == "__main__":
    sys.exit(main())
