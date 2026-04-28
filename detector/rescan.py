# detector/rescan.py
"""CG-1 cloud-vs-state rescan -- the unmanaged-resource tracking entry point.

Implements the headline CG-1 capability: surface resources in cloud that
are NOT in Terraform state. Without this, the Detector engine is a
strict subset of ``terraform plan`` -- it only sees the resources state
already tracks. WITH this, the Detector closes the
discover -> codify -> baseline loop that Firefly / ControlMonkey use as
their lead feature ("show me unmanaged resources" within the first 5
minutes of any vendor demo).

Public surface (one function):

    rescan(project_id, *, project_root) -> DriftReport

Reads-only:
  * Calls importer.inventory.inventory(project_id, raise_on_error=False)
    to enumerate every importer-supported asset type.
  * Calls state_reader.read_state(state_path) to enumerate everything
    Terraform knows about for this project.
  * Diffs the two sets to find unmanaged (in cloud, not in state).

Does NOT:
  * Run terraform plan (drift_check is future work; rescan() is the
    cheap unmanaged-only operation).
  * Mutate state or call gcloud delete / terraform destroy / etc.
  * Make decisions about what to do with unmanaged resources -- it
    just reports them. The UI's "Codify this" button hands off to the
    importer's writer.

P4-3 scope: unmanaged tracking only. drifted / compliant buckets in
the returned DriftReport are populated from state-only data:
  * compliant = every state resource (treated as nominally
                compliant since no drift check ran)
  * drifted   = []  (drift check is opt-in via future drift_check arg)
A future commit will wire drift_check=True to call cloud_snapshot +
diff_engine on each in-state resource and reclassify
compliant -> drifted as appropriate. The DriftReport shape supports
this without further refactoring.

Matching rule: cloud-vs-state identity is the tuple
``(tf_type, normalized_short_name)``. Both sides go through the
same normalization (URN-style names collapse to last path segment via
gcp_client.friendly_name_from_display) so URN-bearing types
(KMS / Pub/Sub) match correctly.
"""

from __future__ import annotations

import os
import time
from typing import List

from importer.inventory import CloudResource, inventory as _inventory
from importer.gcp_client import friendly_name_from_display
from common.errors import PreflightError
from common.logging import get_logger

from . import config, state_reader
from .drift_report import DriftReport
from .state_reader import ManagedResource

_log = get_logger(__name__)


def _normalized_state_name(resource: ManagedResource) -> str:
    """Extract the short cloud-side name from a ManagedResource.

    State stores the cloud name in ``attributes["name"]`` for most
    resource types. For URN-bearing types (KMS keyrings, Pub/Sub
    topics) the state attribute may carry the full URN; we apply the
    same friendly_name_from_display normalization the inventory()
    side uses so the match key is symmetric.
    """
    raw = resource.attributes.get("name", "")
    return friendly_name_from_display(raw) or ""


def _build_unmanaged(
    cloud_resources: List[CloudResource],
    state_resources: List[ManagedResource],
) -> List[CloudResource]:
    """Set-diff: cloud_resources - state_resources by (tf_type, name).

    Pure function; no I/O. The match key is
    ``(tf_type, normalized_short_name)`` -- both sides apply
    friendly_name_from_display so URN vs short-name doesn't cause
    false-positive "unmanaged" entries.

    Returns:
        Sorted list of CloudResource entries that are in cloud but
        not in state. Sorted by (tf_type, cloud_name) for determinism.
    """
    state_keys = {
        (r.tf_type, _normalized_state_name(r))
        for r in state_resources
    }
    unmanaged = [
        c for c in cloud_resources
        if (c.tf_type, c.cloud_name) not in state_keys
    ]
    # CloudResource is already sorted by inventory() -- but defensively
    # re-sort in case caller passed a re-ordered list.
    unmanaged.sort(key=lambda r: (r.tf_type, r.cloud_name))
    return unmanaged


def rescan(project_id: str, *, project_root: str) -> DriftReport:
    """Cloud-vs-state rescan; returns a structured DriftReport.

    P4-3 scope: enumerates the cloud, reads state, set-diffs to find
    unmanaged. Does NOT run terraform plan -- drifted / compliant
    buckets reflect the simpler "all in state = compliant" model
    pending future drift_check wiring.

    Args:
        project_id: GCP project to rescan. Caller is responsible for
            ADC / SA impersonation setup.
        project_root: Per-project workdir absolute path. Required;
            no silent cwd fallback (P4-1 detector hygiene contract).
            State file is read from
            ``<project_root>/<config.STATE_FILE_NAME>``.

    Returns:
        DriftReport with three buckets populated:
          * unmanaged: in cloud, not in state (the CG-1 finding)
          * compliant: all state resources (no drift check ran)
          * drifted:   [] (drift check is future work)
        Plus inventory_errors carrying any asset-types whose
        enumeration failed (so the UI can warn that the unmanaged
        report may be incomplete).

    Raises:
        PreflightError: project_root is missing / unreadable. Same
            stage tag (`resolve_workdir`) as detector.remediator's
            _state_path() so dashboards filter both with one query.
    """
    if not project_root:
        raise PreflightError(
            "rescan() called without project_root; refusing to fall "
            "back to process cwd (would risk wrong-tenant state reads "
            "under concurrency).",
            stage="resolve_workdir",
            reason="missing_project_root_arg",
        )
    if not os.path.isdir(project_root):
        raise PreflightError(
            f"rescan() project_root does not exist: {project_root}",
            stage="resolve_workdir",
            reason="project_root_not_a_directory",
        )

    log = _log.bind(project_id=project_id, op="rescan")
    log.info("rescan_start", project_root=project_root)
    started = time.monotonic()

    # Cloud side: full inventory. raise_on_error=False keeps the rescan
    # best-effort -- per-asset-type failures are recorded in
    # inventory_errors so the UI can warn but the rescan still
    # returns a partial report rather than blowing up.
    inventory_errors: List[str] = []
    try:
        cloud_resources = _inventory(project_id, raise_on_error=False)
    except Exception as exc:  # noqa: BLE001 -- log + record + continue
        # Catastrophic failure during enumeration (rare under
        # raise_on_error=False but defensive). Treat as zero cloud
        # resources discovered + record an aggregate error.
        log.error("rescan_inventory_catastrophic_failure",
                  error_type=type(exc).__name__, error=str(exc))
        cloud_resources = []
        inventory_errors.append(f"inventory_call_failed: {exc}")

    # State side: read tfstate from per-project workdir.
    state_path = os.path.join(project_root, config.STATE_FILE_NAME)
    state_resources = state_reader.read_state(state_path)

    # Diff: unmanaged = cloud - state (by (tf_type, name) key).
    unmanaged = _build_unmanaged(cloud_resources, state_resources)

    # Bucket assignment for in-state resources. P4-3: all go to
    # compliant (no drift check ran). drifted=[] until drift_check
    # is wired in a future commit.
    compliant = list(state_resources)
    drifted: List[ManagedResource] = []

    elapsed = time.monotonic() - started
    report = DriftReport(
        project_id=project_id,
        drifted=drifted,
        compliant=compliant,
        unmanaged=unmanaged,
        inventory_errors=inventory_errors,
        duration_s=round(elapsed, 2),
    )
    log.info("rescan_complete", **report.as_fields())

    # PSA-9: persist snapshot for Dashboard. Best-effort -- a snapshot
    # write failure (network, perms, env-gate off) MUST NOT take down
    # the engine. The detector already logged its result above; the
    # GCS snapshot is purely for the Dashboard's cached read path.
    try:
        from common.snapshots import write_snapshot
        write_snapshot("detector", report.as_fields(), project_id)
    except Exception as snap_err:
        log.warning(
            "snapshot_write_skipped", engine="detector",
            error=str(snap_err),
            reason="snapshot persistence failed; engine result unaffected",
        )

    return report
