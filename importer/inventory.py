# importer/inventory.py
"""Reusable cloud-side resource enumeration.

Extracted in P4-2 (CG-1 Phase 4) from ``run_workflow``'s Stage 1
discovery loop. Pre-P4-2 the enumeration was inline in
``importer/run.py:run_workflow`` -- only callable by walking through
the importer's full HITL CLI flow. The Detector engine (P4-3 next)
needs the same enumeration to surface UNMANAGED resources (in cloud,
not in state) -- a CG-1 capability that requires a clean reusable
entry point.

Public surface (one function + one dataclass + one exception):

  * :class:`CloudResource` -- frozen dataclass; the cloud-side
    counterpart to ``detector.state_reader.ManagedResource``. P4-3's
    ``DriftReport`` will diff a ``set[CloudResource]`` against a
    ``set[ManagedResource]`` to identify the unmanaged bucket.
  * :func:`inventory` -- read-only enumeration over every importer-
    supported asset type for a project. Same parallelism + error
    surface as the original Stage 1 loop, with the addition of an
    optional ``raise_on_error`` flag for callers that need strict
    completeness guarantees.
  * :class:`InventoryError` -- raised by ``inventory(strict=True)``
    when one or more asset-type enumerations failed. Local to this
    module rather than common.errors per the "promote when 2+
    engines need it" guidance in common/errors.py.

Importer side (back-compat): ``run_workflow`` now calls
``inventory(project_id)`` instead of inlining the parallel loop.
The downstream ``_present_selection_menu`` + ``_map_asset_to_terraform``
pipeline still receives a ``list[dict]`` (the raw gcloud asset JSON);
``inventory()`` exposes it via :attr:`CloudResource.raw_asset` so the
adapter is one ``[r.raw_asset for r in inventory_result]`` list
comprehension. Zero behavior change for the importer.

Detector side (forthcoming P4-3): can call
``inventory(project_id, raise_on_error=True)`` to get a strict
guarantee that the unmanaged-resource report is complete (vs partial
because some asset-type enumeration failed silently). The Detector's
UI surface will distinguish "0 unmanaged" from "0 unmanaged but
discovery was incomplete on N asset types".
"""

from __future__ import annotations

import concurrent.futures
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from . import config, gcp_client
from common.logging import get_logger

_log = get_logger(__name__)


@dataclass(frozen=True)
class CloudResource:
    """A resource discovered via ``gcloud asset search-all-resources``.

    Cloud-side counterpart to ``detector.state_reader.ManagedResource``.
    P4-3's drift report will set-diff these to find UNMANAGED resources
    (in cloud, not in state).

    Frozen so instances are hashable and usable in ``set()`` for the
    diff. ``raw_asset`` is excluded from hash via ``compare=False`` --
    two CloudResource instances are "the same resource" iff their
    identifying fields match, regardless of whether downstream
    metadata differs.

    Attributes:
        tf_type: The Terraform resource type this asset maps to
            (e.g. ``"google_compute_instance"``). Sourced from
            ``importer.config.ASSET_TO_TERRAFORM_MAP``.
        asset_type: The raw GCP asset type
            (e.g. ``"compute.googleapis.com/Instance"``).
        cloud_name: The short, human-friendly name of the resource
            (e.g. ``"poc-vm"``). Normalized via
            ``gcp_client.friendly_name_from_display`` so URN-style
            displayName values (KMS keyrings, Pub/Sub topics) collapse
            to their last path segment. This is the value a customer
            would recognize in the GCP Console.
        cloud_urn: The full canonical name / path
            (e.g. ``"projects/.../zones/us-central1-a/instances/poc-vm"``).
            Used by gcloud describe and as the identity key for diff
            against state (the state file's attribute paths usually
            reference a similar URN).
        project_id: The GCP project this resource lives in. Repeated
            on every CloudResource so cross-project enumerations
            (future) can carry per-resource provenance.
        location: Zone / region / global, when the asset reports one.
            ``None`` for project-scoped resources (IAM SA, project-
            level KMS keyring).
        raw_asset: The complete dict returned by gcloud asset
            search-all-resources. Preserved for downstream
            consumers (``_map_asset_to_terraform`` in importer/run.py
            still wants the full shape; UI rendering can pull
            additional fields without re-fetching). Excluded from
            equality / hash so identical-resource-with-different-
            metadata-snapshots compare equal.
    """
    tf_type: str
    asset_type: str
    cloud_name: str
    cloud_urn: str
    project_id: str
    location: Optional[str] = None
    raw_asset: dict = field(default_factory=dict, compare=False, hash=False)


class InventoryError(Exception):
    """Raised by ``inventory(raise_on_error=True)`` when one or more
    asset-type enumerations failed.

    Carries the list of failed asset types and the project_id so a
    caller (typically the Detector with its strict-completeness need)
    can decide whether to retry, narrow the scope, or surface to the
    operator with actionable detail.

    Local to importer.inventory rather than common.errors per the
    "promote when 2+ engines need it" guidance. If a future engine
    needs typed inventory failures, lift this up to common/errors.py
    at that point.
    """

    def __init__(self, message: str, *, project_id: str,
                 failed_asset_types: list[str]):
        super().__init__(message)
        self.project_id = project_id
        self.failed_asset_types = failed_asset_types


def _to_cloud_resource(raw: dict, project_id: str,
                       asset_type: str, tf_type: str) -> CloudResource:
    """Normalize one gcloud asset JSON dict into a CloudResource.

    Pure function; no I/O. Reuses
    :func:`gcp_client.friendly_name_from_display` to handle the
    URN-as-displayName case (CC-8 P2-6 fix) so KMS keyrings and
    Pub/Sub topics carry a clean short name in ``cloud_name``.
    """
    raw_display = raw.get("displayName") or raw.get("name") or ""
    return CloudResource(
        tf_type=tf_type,
        asset_type=asset_type,
        cloud_name=gcp_client.friendly_name_from_display(raw_display) or "",
        cloud_urn=raw.get("name", ""),
        project_id=project_id,
        location=raw.get("location"),
        raw_asset=raw,
    )


def inventory(
    project_id: str,
    *,
    raise_on_error: bool = False,
) -> list[CloudResource]:
    """Read-only enumeration of every importer-supported resource in a project.

    Same parallel-discovery shape as ``run_workflow``'s Stage 1: a
    ``ThreadPoolExecutor`` fans out one ``discover_resources_of_type``
    call per asset_type in :data:`config.ASSET_TO_TERRAFORM_MAP`,
    bounded by :data:`config.MAX_DISCOVERY_WORKERS`.

    Determinism: results are sorted by ``(tf_type, cloud_name)`` so the
    output is stable across runs (useful for set diffing in the
    Detector and for snapshot-based golden tests).

    Error semantics:
      * Default (``raise_on_error=False``): per-asset-type failures
        are logged at WARN with the asset_type + error and the loop
        continues. Returns whatever resources were enumerated
        successfully. Matches the importer's pre-P4-2 best-effort
        behavior so the importer's behavior is unchanged.
      * Strict (``raise_on_error=True``): if ANY asset_type fails,
        the function raises :class:`InventoryError` carrying the
        failed asset types. Use this from the Detector's drift /
        unmanaged-tracking path where partial inventory means
        false-negatives ("0 unmanaged" when really we couldn't
        even check N types).

    Args:
        project_id: The GCP project to enumerate. Caller must ensure
            ADC / SA impersonation is set up so gcloud can read the
            project's Cloud Asset Inventory.
        raise_on_error: When True, raise InventoryError on any
            per-asset-type failure. When False (default), log + continue.

    Returns:
        Sorted list of :class:`CloudResource`. Empty list if the
        project has no supported resources. Sorted by
        ``(tf_type, cloud_name)`` for determinism.

    Raises:
        InventoryError: only when ``raise_on_error=True`` and at
            least one asset_type enumeration failed.
    """
    log = _log.bind(project_id=project_id, op="inventory")
    asset_types = list(config.ASSET_TO_TERRAFORM_MAP)
    log.info("inventory_start", asset_types_count=len(asset_types))
    started = time.monotonic()

    results: list[CloudResource] = []
    errors: dict[str, Exception] = {}

    with concurrent.futures.ThreadPoolExecutor(
        max_workers=config.MAX_DISCOVERY_WORKERS,
    ) as executor:
        future_to_at = {
            executor.submit(
                gcp_client.discover_resources_of_type, project_id, at,
            ): at
            for at in asset_types
        }
        for future in concurrent.futures.as_completed(future_to_at):
            asset_type = future_to_at[future]
            try:
                raw_resources = future.result()
            except Exception as exc:  # noqa: BLE001 -- recorded + decided below
                errors[asset_type] = exc
                log.warning(
                    "inventory_asset_type_failed",
                    asset_type=asset_type,
                    error_type=type(exc).__name__,
                    error=str(exc),
                )
                continue

            if not raw_resources:
                continue
            tf_type = config.ASSET_TO_TERRAFORM_MAP[asset_type]
            for raw in raw_resources:
                results.append(_to_cloud_resource(
                    raw, project_id, asset_type, tf_type,
                ))

    elapsed = time.monotonic() - started
    log.info(
        "inventory_complete",
        resource_count=len(results),
        error_count=len(errors),
        duration_s=round(elapsed, 2),
    )

    if raise_on_error and errors:
        raise InventoryError(
            f"inventory failed for {len(errors)} asset type(s): "
            f"{', '.join(sorted(errors))}",
            project_id=project_id,
            failed_asset_types=sorted(errors),
        )

    # Deterministic order: by tf_type then cloud_name. Caller can
    # re-sort (e.g. importer.run.py re-sorts by displayName for the
    # selection menu's human-friendly grouping).
    results.sort(key=lambda r: (r.tf_type, r.cloud_name))
    return results
