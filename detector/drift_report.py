# detector/drift_report.py
"""Structured return type for ``Detector.rescan()``.

Mirrors the importer's ``WorkflowResult`` (CC-4) shape and intent:
callers get a frozen dataclass with three structured buckets + counts,
not a list of mixed log lines. The third bucket -- ``unmanaged`` -- is
the headline CG-1 capability: resources in the cloud account that are
NOT in Terraform state. Without it, a Detector pass is a strict subset
of ``terraform plan`` (it only sees the resources state already
tracks); WITH it, the Detector closes the discover -> codify loop that
Firefly / ControlMonkey use as their lead feature.

Three-bucket shape per the punchlist CG-1 spec:

    drifted   : in state, cloud values differ from .tf
                (populated by terraform plan; left empty by rescan()
                in P4-3 -- a future commit / explicit drift_check=True
                will populate this)
    compliant : in state, cloud matches .tf
                (populated by terraform plan; treated as the default
                "in-state" bucket in P4-3 since no drift check runs)
    unmanaged : in cloud, NOT in state                              (NEW)
                (populated unconditionally by rescan())

Why mirror WorkflowResult instead of inventing a new shape: operators
reading per-engine reports in the same dashboard see one consistent
result shape across importer.WorkflowResult,
translator.TranslationResult, and (with this commit)
detector.DriftReport. Same field-name conventions (counts as
top-level ints, lists for per-item detail, ``as_fields()`` for the
structured-log payload), same ``exit_code`` semantics so CI
orchestrators wrap any of them with one rule.

A+D return contract (same as importer + translator):
  * Per-resource diff results -> entry in the appropriate bucket,
    workflow completes, DriftReport returned.
  * Inputs/environment failures (project_id invalid, state file
    unreadable, inventory completely failed under raise_on_error=True)
    -> raise PreflightError / InventoryError, no DriftReport returned.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, List

from importer.inventory import CloudResource

from .diff_engine import ResourceDrift
from .state_reader import ManagedResource


@dataclass(frozen=True)
class DriftReport:
    """Outcome of a single ``Detector.rescan()`` invocation.

    Counts derive from the per-bucket lists -- they're stored
    explicitly for fast dashboard rollups (Cloud Logging filters on
    ``unmanaged_count`` etc. without iterating the array each time).

    Attributes:
        project_id: The GCP project the rescan was run against.
        drifted: Resources in state whose cloud values differ from the
            recorded .tf. Populated when rescan() is called with
            ``drift_check=True`` (PUI-4e). Each entry is a
            ``ResourceDrift`` carrying per-field DriftItems (path,
            op, state_value, cloud_value) -- the SaaS Detector page
            renders these in a side-by-side cloud-vs-state viewer.
            Left empty when ``drift_check=False`` (cheap-rescan mode).
        compliant: Resources in state whose cloud values match .tf.
            With ``drift_check=False``: all in-state resources land
            here (nominally compliant). With ``drift_check=True``:
            only resources whose per-field diff came back clean.
        unmanaged: Resources in the cloud that are NOT in state. The
            CG-1 capability: customer adopts 16 resources Monday, an
            admin spins up a new bucket Tuesday in the console, our
            Tuesday rescan surfaces that bucket here. UI's "Codify
            this" action takes one of these and hands it to the
            importer's writer.
        inventory_errors: Asset types that failed enumeration during
            the cloud-side discovery. Empty list = complete inventory;
            non-empty = the unmanaged bucket may have false negatives
            (resources of the failed types couldn't even be checked).
            UI surfaces this so customers don't false-trust an "0
            unmanaged" report when discovery was actually incomplete.
        duration_s: Total wall-clock from rescan start to return.
    """

    project_id: str
    drifted: List[ResourceDrift] = field(default_factory=list)
    compliant: List[ManagedResource] = field(default_factory=list)
    unmanaged: List[CloudResource] = field(default_factory=list)
    inventory_errors: List[str] = field(default_factory=list)
    duration_s: float = 0.0
    # PUI-2pre gap #5 (2026-04-30): orphan-vs-child split + Coverage %.
    # Pre-PUI-2pre this lived as inline JS-style code in
    # app/pages/3_Drift_Detection.py:_classify_parent_owner.
    # Hoisted into the engine so:
    #   (a) the snapshot persists the orphan-filtered numbers for
    #       the Dashboard's Coverage % hero metric (PUI-2),
    #   (b) the Drift Detection page reads pre-computed counts
    #       instead of re-classifying on every render, and
    #   (c) future engines (Policy, etc.) can reuse the classifier
    #       without page-side duplication.
    # `unmanaged_orphan_count + unmanaged_child_count` always equals
    # `len(unmanaged)`. `coverage_pct` is rounded to nearest int and
    # uses the Firefly-parity denominator (in_state + orphan).
    unmanaged_orphan_count: int = 0
    unmanaged_child_count: int = 0
    coverage_pct: int = 0
    # PUI-2pre gap #6: per-tf_type discovery counts. Powers the
    # Dashboard's Inventory card "discovered by tf_type (top 5)"
    # without an extra GCS write. Single source of truth: the
    # detector's rescan already enumerates the cloud once via
    # importer.inventory.inventory().
    discovered_by_type: dict = field(default_factory=dict)

    # D-4 fix (2026-04-28): expose the per-bucket counts as properties
    # so callers can write `report.compliant_count` (the natural name)
    # instead of `len(report.compliant)`. Pre-fix the *_count names
    # only existed in the as_fields() dict, so every operator hitting
    # the obvious accessor (we hit this twice during SMOKE 4) got an
    # AttributeError. Properties mirror the dict keys exactly.
    @property
    def drifted_count(self) -> int:
        """Number of state resources that drifted from cloud."""
        return len(self.drifted)

    @property
    def compliant_count(self) -> int:
        """Number of state resources whose cloud values match .tf."""
        return len(self.compliant)

    @property
    def unmanaged_count(self) -> int:
        """Number of cloud resources NOT in state (CG-1 metric)."""
        return len(self.unmanaged)

    @property
    def inventory_error_count(self) -> int:
        """Number of asset types that failed enumeration."""
        return len(self.inventory_errors)

    @property
    def total_in_state(self) -> int:
        """Resources tracked by Terraform (drifted + compliant)."""
        return len(self.drifted) + len(self.compliant)

    @property
    def total_in_cloud(self) -> int:
        """Resources visible in the cloud (in_state + unmanaged)."""
        return self.total_in_state + len(self.unmanaged)

    @property
    def exit_code(self) -> int:
        """0 iff fully clean: no drift, no unmanaged, no inventory errors.

        Mirrors WorkflowResult.exit_code semantics. CI orchestrators
        wrapping the Detector can treat any non-zero exit as "human
        review required".
        """
        if self.drifted or self.unmanaged or self.inventory_errors:
            return 1
        return 0

    def as_fields(self) -> dict[str, Any]:
        """Flat dict for structured-log emission. Excludes the heavy
        per-bucket lists -- per-resource detail is logged via separate
        events during the rescan, not re-emitted here. Same
        convention as WorkflowResult.as_fields and
        TranslationResult.as_fields.

        PUI-2pre (2026-04-30) added orphan/child split,
        coverage_pct, and discovered_by_type so the Dashboard
        (PUI-2) can render Coverage % + per-tf_type breakdown
        from the snapshot alone (no re-classification on read).
        """
        d: dict[str, Any] = {
            "project_id": self.project_id,
            "drifted_count": len(self.drifted),
            "compliant_count": len(self.compliant),
            "unmanaged_count": len(self.unmanaged),
            "inventory_error_count": len(self.inventory_errors),
            "duration_s": self.duration_s,
            "exit_code": self.exit_code,
            # PUI-2pre gap #5 + #6: snapshot the orphan-filtered
            # counts + coverage + discovery-by-type so the Dashboard
            # reads pre-computed values from snapshots/<engine>/latest.
            "unmanaged_orphan_count": self.unmanaged_orphan_count,
            "unmanaged_child_count": self.unmanaged_child_count,
            "coverage_pct": self.coverage_pct,
            "discovered_by_type": dict(self.discovered_by_type),
        }
        return d
