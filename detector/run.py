# detector/run.py
"""
On-demand CLI for drift detection (POC).

Usage:
    python -m detector.run

Reads ./terraform.tfstate, fetches live cloud JSON for the in-scope resources,
prints a structured drift report, and exits non-zero if drift was found.
"""

import os
import sys

from . import config, state_reader, cloud_snapshot, diff_engine, remediator


def main() -> int:
    # Always operate from the project root so paths line up with the importer.
    project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    os.chdir(project_root)
    print(f"--- Working directory: {os.getcwd()} ---")

    state_path = os.path.join(project_root, config.STATE_FILE_NAME)
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
        drifts.append(diff_engine.diff_resource(
            tf_address=r.tf_address,
            tf_type=r.tf_type,
            state_attrs=r.attributes,
            cloud_json=snapshots.get(r.tf_address),
        ))

    drift_count = diff_engine.print_report(drifts)

    # If we found drift AND we're sitting at an interactive terminal, walk
    # the user through remediation. The remediator no-ops in CI / non-tty
    # contexts so the exit code below remains the canonical CI signal.
    if drift_count > 0:
        remediator.run_remediation(drifts)

    return 1 if drift_count > 0 else 0


if __name__ == "__main__":
    sys.exit(main())