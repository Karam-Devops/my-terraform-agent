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

from . import cloud_snapshot, config, diff_engine, state_reader
from .diff_engine import ResourceDrift
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


def _classify_parent_owner(
    resource: CloudResource,
    *,
    compliant_cluster_names: set,
    compliant_vm_names: set,
    compliant_keyring_names: set,
    compliant_topic_names: set,
) -> str:
    """Return human-readable parent ownership tag, or empty string
    if the resource is genuinely orphan.

    PUI-2pre gap #5 (2026-04-30): hoisted from
    app/pages/3_Drift_Detection.py:_classify_parent_owner so
    the engine produces the orphan-vs-child split natively (the
    Dashboard's Coverage % hero metric needs the orphan-filtered
    numerator from the snapshot, not from page-side recomputation).

    Heuristics (ordered most-specific to least):
      * GKE node-pool VMs / boot disks / NEGs -- prefix `gke-`
        (reserved by GKE; safe unconditional match)
      * Default project service accounts -- well-known @-domains
        + numeric-prefix locals
      * GCE auto-spawned boot disks -- name matches a managed VM
      * KMS keys nested under a managed keyring -- URN check
      * Pub/Sub subscriptions nested under a managed topic
      * Default VPC / subnetwork (name == "default")

    Returns the human-readable tag string when classified as a
    child, empty string when genuinely orphan. Page renders the
    tag verbatim; Dashboard just counts orphans-vs-children.

    Argument shape mirrors the page's pre-PUI-2pre pre-built sets
    so callers can pass cached lookup sets if classifying many
    resources in a tight loop.
    """
    name = (resource.cloud_name or "")
    urn = (getattr(resource, "cloud_urn", "") or "")
    tf_type = resource.tf_type

    # GKE auto-spawn: node-pool VMs, boot disks, NEGs, instance
    # groups, etc. All have name prefix `gke-`. Reserved by GKE
    # so unconditional matching is safe.
    if name.startswith("gke-"):
        for cluster in compliant_cluster_names:
            cluster_hyphen = cluster.replace("_", "-")
            if (
                name.startswith(f"gke-{cluster}-")
                or name.startswith(f"gke-{cluster_hyphen}-")
            ):
                return f"GKE cluster `{cluster_hyphen}`"
        return "GKE cluster (not yet imported)"

    # Default project service accounts.
    if tf_type == "google_service_account":
        sa_default_domains = (
            "@cloudservices.gserviceaccount.com",
            "@developer.gserviceaccount.com",
            "@cloudbuild.gserviceaccount.com",
            "@compute-system.iam.gserviceaccount.com",
            "@container-engine-robot.iam.gserviceaccount.com",
            "@gcp-sa-",  # any "gcp-sa-*" Google-managed SA
            "@dataproc-accounts.iam.gserviceaccount.com",
            "@cloud-tpu.iam.gserviceaccount.com",
        )
        if any(d in urn or d in name for d in sa_default_domains):
            return "GCP default service account"
        local = name.split("@", 1)[0] if "@" in name else name
        if local.split("-")[0].isdigit():
            return "GCP default service account"
        if local.endswith("-compute") or local == "default":
            return "GCP default service account"

    # GCE auto-created boot disk.
    if tf_type == "google_compute_disk":
        if name in compliant_vm_names:
            return f"VM `{name}` (boot disk)"
        for vm in compliant_vm_names:
            if name == vm.replace("_", "-"):
                return f"VM `{vm.replace('_', '-')}` (boot disk)"

    # KMS keys nested under a managed keyring.
    if tf_type == "google_kms_crypto_key":
        for keyring in compliant_keyring_names:
            if (
                f"/keyRings/{keyring}/" in urn
                or f"/keyRings/{keyring.replace('_', '-')}/" in urn
            ):
                return f"KMS key ring `{keyring.replace('_', '-')}`"

    # Pub/Sub subscriptions nested under a managed topic.
    if tf_type == "google_pubsub_subscription":
        for topic in compliant_topic_names:
            topic_hyphen = topic.replace("_", "-")
            if (
                f"/topics/{topic}" in urn
                or f"/topics/{topic_hyphen}" in urn
                or topic in name
                or topic_hyphen in name
            ):
                return f"Pub/Sub topic `{topic_hyphen}`"

    # Default networks/subnets that GCP creates per-project.
    if tf_type in ("google_compute_network", "google_compute_subnetwork"):
        if name == "default":
            return "GCP default VPC"

    return ""  # genuinely orphan


def _partition_unmanaged(
    unmanaged: List[CloudResource],
    state_resources: List[ManagedResource],
) -> tuple[List[CloudResource], List[CloudResource]]:
    """Split unmanaged into (orphan, child) based on parent heuristics.

    PUI-2pre gap #5: produces the orphan-vs-child split that the
    Dashboard's Coverage % hero metric needs. Pure function -- no
    I/O. Both buckets together always equal the input list.

    Looks up ``state_resources`` for the well-known parent types
    (clusters, VMs, keyrings, topics) so the heuristic can attribute
    a child to a SPECIFIC managed parent rather than just "auto-managed
    by something".
    """
    compliant_cluster_names = {
        r.hcl_name for r in state_resources
        if r.tf_type == "google_container_cluster"
    }
    compliant_vm_names = {
        r.hcl_name for r in state_resources
        if r.tf_type == "google_compute_instance"
    }
    compliant_keyring_names = {
        r.hcl_name for r in state_resources
        if r.tf_type == "google_kms_key_ring"
    }
    compliant_topic_names = {
        r.hcl_name for r in state_resources
        if r.tf_type == "google_pubsub_topic"
    }

    orphans: List[CloudResource] = []
    children: List[CloudResource] = []
    for r in unmanaged:
        owner = _classify_parent_owner(
            r,
            compliant_cluster_names=compliant_cluster_names,
            compliant_vm_names=compliant_vm_names,
            compliant_keyring_names=compliant_keyring_names,
            compliant_topic_names=compliant_topic_names,
        )
        if owner:
            children.append(r)
        else:
            orphans.append(r)
    return orphans, children


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


def _classify_in_state(
    state_resources: List[ManagedResource],
    *,
    log,
) -> tuple[List[ResourceDrift], List[ManagedResource]]:
    """PUI-4e: per-resource cloud-vs-state diff for in-state resources.

    Fetches a fresh cloud snapshot for every in-scope state resource
    and runs ``diff_engine.diff_resource()`` to partition them into
    ``drifted`` (per-field deltas exist) and ``compliant`` (in sync).

    Out-of-scope state resources skip the diff (no normalization rules
    available) and stay in compliant -- mirrors the CLI behavior.

    Drift-stub types (in-scope but lacking full normalization rules)
    are HIDDEN from the drifted bucket per Firefly UX parity (we
    don't surface "monitored, conservative" caveats to the customer).
    They land in compliant unless a real diff fires.

    Snapshot fetching reuses ``cloud_snapshot.fetch_snapshots`` which
    is already threadpooled at ``MAX_SNAPSHOT_WORKERS`` (8 by
    default). For a 50-resource project this is ~30-60s wall clock;
    the SaaS Detector page caches the result so subsequent renders
    don't re-pay the cost.

    Args:
        state_resources: Output of ``state_reader.read_state``.
        log: Bound logger from the caller (so progress events nest
            under the same op="rescan" structured field).

    Returns:
        ``(drifted, compliant)`` partition of the input list.
    """
    drifted: List[ResourceDrift] = []
    compliant: List[ManagedResource] = []

    in_scope = [r for r in state_resources if r.in_scope]
    out_of_scope = [r for r in state_resources if not r.in_scope]
    # Out-of-scope types stay compliant unconditionally (no diff
    # available). Mirrors CLI behavior at detector/run.py:132.
    compliant.extend(out_of_scope)

    if not in_scope:
        log.info("rescan_drift_check_skipped",
                 reason="no in-scope state resources to diff")
        return drifted, compliant

    log.info("rescan_drift_check_start",
             in_scope_count=len(in_scope),
             out_of_scope_count=len(out_of_scope))

    # Fetch parallel cloud snapshots (already threadpooled).
    snapshots = cloud_snapshot.fetch_snapshots(in_scope)

    error_count = 0
    drift_stub_count = 0
    for r in in_scope:
        # Drift-stub gating: types in-scope but without full
        # normalization rules would produce noisy false-positive
        # drift. Per Firefly UX parity (Hide drift-stubs) we treat
        # these as compliant unconditionally rather than rendering
        # a "monitored, conservative" caveat in the SaaS UI. The CLI
        # surfaces drift_stub=True placeholders for power-user
        # transparency; the SaaS does not.
        if not config.is_drift_aware(r.tf_type):
            compliant.append(r)
            drift_stub_count += 1
            continue

        drift = diff_engine.diff_resource(
            tf_address=r.tf_address,
            tf_type=r.tf_type,
            state_attrs=r.attributes,
            cloud_json=snapshots.get(r.tf_address),
        )
        if drift.has_drift:
            drifted.append(drift)
            if drift.error:
                error_count += 1
        else:
            compliant.append(r)

    log.info("rescan_drift_check_complete",
             drifted_count=len(drifted),
             compliant_count=len(compliant),
             error_count=error_count,
             drift_stub_hidden_count=drift_stub_count)
    return drifted, compliant


def rescan(
    project_id: str,
    *,
    project_root: str,
    drift_check: bool = False,
) -> DriftReport:
    """Cloud-vs-state rescan; returns a structured DriftReport.

    Two operating modes:
      * ``drift_check=False`` (default, cheap): enumerates cloud,
        reads state, set-diffs to find unmanaged. All in-state
        resources land in ``compliant``; ``drifted`` stays empty.
        Wall-clock ~5-15s for a 50-resource project.
      * ``drift_check=True`` (PUI-4e, expensive): also runs
        ``diff_engine.diff_resource()`` per in-scope state resource
        to partition compliant -> drifted on real per-field deltas.
        Wall-clock ~30-90s for a 50-resource project.

    Args:
        project_id: GCP project to rescan. Caller is responsible for
            ADC / SA impersonation setup.
        project_root: Per-project workdir absolute path. Required;
            no silent cwd fallback (P4-1 detector hygiene contract).
            State file is read from
            ``<project_root>/<config.STATE_FILE_NAME>``.
        drift_check: When True, runs the per-resource diff and
            populates ``DriftReport.drifted`` with ResourceDrift
            entries (each carrying DriftItems for the SaaS Detector
            page's side-by-side viewer). Default False keeps the
            cheap-rescan contract for callers that only want
            unmanaged tracking.

    Returns:
        DriftReport with three buckets populated:
          * unmanaged: in cloud, not in state (the CG-1 finding)
          * compliant: in-state resources whose cloud matches HCL
            (or all in-state resources when ``drift_check=False``)
          * drifted: in-state resources with per-field deltas
            (populated only when ``drift_check=True``)
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
    log.info("rescan_start", project_root=project_root,
             drift_check=drift_check)
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

    # PUI-2pre gap #5: orphan-vs-child split for Coverage % hero.
    # Engine-side classification so the snapshot persists pre-computed
    # counts; Dashboard reads them directly without re-classifying.
    unmanaged_orphans, unmanaged_children = _partition_unmanaged(
        unmanaged, state_resources,
    )

    # PUI-2pre gap #6: per-tf_type discovery counts. Powers the
    # Dashboard's Inventory card "discovered by tf_type (top 5)"
    # without an extra GCS write. Single source of truth: the
    # cloud_resources we already enumerated above.
    discovered_by_type: dict = {}
    for r in cloud_resources:
        discovered_by_type[r.tf_type] = discovered_by_type.get(r.tf_type, 0) + 1

    # Bucket assignment for in-state resources. PUI-4e: when
    # drift_check is True, run the per-resource diff to partition
    # compliant vs drifted. Otherwise keep the cheap default
    # (everything in-state = compliant).
    if drift_check:
        drifted, compliant = _classify_in_state(state_resources, log=log)
    else:
        compliant = list(state_resources)
        drifted: List[ResourceDrift] = []

    elapsed = time.monotonic() - started
    # PUI-2pre Coverage % calc (Firefly parity): denominator excludes
    # auto-managed children since those aren't IaC-eligible.
    _in_state = len(compliant) + len(drifted)
    _iac_eligible = _in_state + len(unmanaged_orphans)
    coverage_pct = (
        round(100.0 * _in_state / _iac_eligible) if _iac_eligible > 0 else 0
    )
    report = DriftReport(
        project_id=project_id,
        drifted=drifted,
        compliant=compliant,
        unmanaged=unmanaged,
        inventory_errors=inventory_errors,
        duration_s=round(elapsed, 2),
        # PUI-2pre gap #5 + #6: hoisted classification counts.
        unmanaged_orphan_count=len(unmanaged_orphans),
        unmanaged_child_count=len(unmanaged_children),
        coverage_pct=coverage_pct,
        discovered_by_type=discovered_by_type,
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
