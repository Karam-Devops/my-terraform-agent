# policy/policy_report.py
"""Structured return type for ``policy.scan.scan()``.

Mirrors the importer's ``WorkflowResult`` (CC-4) and the detector's
``DriftReport`` (P4-3) shape and intent:

  * Frozen dataclass with structured per-resource lists + count
    properties.
  * ``as_fields()`` for structured-log emission (excludes the heavy
    per-resource lists; per-finding detail logged by the engine
    during the scan).
  * ``exit_code`` mirroring CI orchestration semantics: 0 iff no
    HIGH-severity violations, 1 otherwise.

Why mirror the existing engine result types: operators reading
per-engine reports in the same dashboard see one consistent result
shape across:
  * importer.WorkflowResult
  * translator.TranslationResult
  * detector.DriftReport
  * policy.PolicyReport (this file)

Same field-name conventions (counts as top-level properties, lists
for per-item detail, ``as_fields()`` for the structured-log payload),
same ``exit_code`` semantics so a single CI rule can wrap any of them.

PUI-5b1 (2026-04-30): introduced as part of the Policy engine refactor
that adds the SaaS-callable ``policy.scan.scan(project_id, *, project_root)
-> PolicyReport`` programmatic entry point. Pre-PUI-5b1 the Policy engine
had only a CLI entry point (``policy/run.py:main()``) which built the
per-resource dict inline and threw it away after pretty-printing.
The Streamlit Detector page can now render Policy findings via this
structured object instead of re-parsing stdout.

Defenses preserved from the CLI (see policy/scan.py module docstring
for the full enumeration):
  * Per-run cap (``cap_hit`` field) -- callers MUST surface this when
    True; otherwise operators silently miss findings on large projects.
  * Severity rollups -- ``high_count`` / ``med_count`` / ``low_count``
    derived from ``per_resource``, not separately maintained, so they
    can never disagree.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List

from .engine import Violation


@dataclass(frozen=True)
class PolicyReport:
    """Outcome of a single ``policy.scan.scan()`` invocation.

    Attributes:
        project_id: The GCP project the scan was run against.
        per_resource: Mapping of TF address (e.g.
            ``google_storage_bucket.poc_bucket``) -> list of
            ``Violation`` objects. Empty list means the resource
            passed all applicable policies. Resources not in the
            ``IN_SCOPE_TF_TYPES`` set do NOT appear in this dict at
            all (mirrors the CLI's behavior of skipping out-of-scope
            types entirely).
        n_resources: Total number of in-scope state resources scanned
            (= ``len(per_resource)``).
        compliant_resources: Number of resources whose violation list
            was empty.
        cap_hit: True iff the per-run violation cap was reached
            (``policy.config.MAX_VIOLATIONS_PER_RUN``, default 1000).
            UI MUST surface a truncation banner when True; otherwise
            operators silently miss findings on large projects or
            when a buggy rule produces an unreasonable count.
        duration_s: Total wall-clock from scan start to return.

    Notes:
        Severity counts (high_count / med_count / low_count) are
        exposed as properties derived from ``per_resource`` rather
        than stored separately. Single source of truth -- they can
        never disagree with the per-resource list.

        ``per_resource`` is a regular dict (not frozen) -- callers
        must NOT mutate it. The frozen dataclass guarantees the
        identity of the dict won't change but Python doesn't have
        a built-in immutable dict type.
    """

    project_id: str
    per_resource: Dict[str, List[Violation]] = field(default_factory=dict)
    n_resources: int = 0
    compliant_resources: int = 0
    cap_hit: bool = False
    duration_s: float = 0.0

    @property
    def violating_resources(self) -> int:
        """Number of resources with one or more violations."""
        return self.n_resources - self.compliant_resources

    @property
    def high_count(self) -> int:
        """Number of HIGH-severity violations across all resources."""
        return sum(
            1 for vs in self.per_resource.values()
            for v in vs if v.severity == "HIGH"
        )

    @property
    def med_count(self) -> int:
        """Number of MED-severity violations across all resources."""
        return sum(
            1 for vs in self.per_resource.values()
            for v in vs if v.severity == "MED"
        )

    @property
    def low_count(self) -> int:
        """Number of LOW-severity violations across all resources."""
        return sum(
            1 for vs in self.per_resource.values()
            for v in vs if v.severity == "LOW"
        )

    @property
    def total_violations(self) -> int:
        """Total violation count across all severities."""
        return sum(len(vs) for vs in self.per_resource.values())

    @property
    def exit_code(self) -> int:
        """0 iff no HIGH violations. CI orchestrators wrap on this.

        Mirrors the importer's WorkflowResult.exit_code +
        detector's DriftReport.exit_code semantics so a single CI
        rule can wrap any engine.
        """
        return 1 if self.high_count > 0 else 0

    def as_fields(self) -> Dict[str, Any]:
        """Flat dict for structured-log emission. Excludes the heavy
        per_resource map (per-finding detail logged by the engine
        during the scan). Same convention as WorkflowResult /
        TranslationResult / DriftReport.
        """
        return {
            "project_id": self.project_id,
            "n_resources": self.n_resources,
            "compliant_resources": self.compliant_resources,
            "violating_resources": self.violating_resources,
            "high_count": self.high_count,
            "med_count": self.med_count,
            "low_count": self.low_count,
            "total_violations": self.total_violations,
            "cap_hit": self.cap_hit,
            "duration_s": self.duration_s,
            "exit_code": self.exit_code,
        }
