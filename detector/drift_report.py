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
            recorded .tf. Populated by terraform plan (left empty by
            rescan() in P4-3 -- drift_check pathway is future work).
        compliant: Resources in state whose cloud values match .tf.
            P4-3 default: all in-state resources land here when no
            drift check runs (treated as nominally compliant).
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
    drifted: List[ManagedResource] = field(default_factory=list)
    compliant: List[ManagedResource] = field(default_factory=list)
    unmanaged: List[CloudResource] = field(default_factory=list)
    inventory_errors: List[str] = field(default_factory=list)
    duration_s: float = 0.0

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
        """
        d: dict[str, Any] = {
            "project_id": self.project_id,
            "drifted_count": len(self.drifted),
            "compliant_count": len(self.compliant),
            "unmanaged_count": len(self.unmanaged),
            "inventory_error_count": len(self.inventory_errors),
            "duration_s": self.duration_s,
            "exit_code": self.exit_code,
        }
        return d
