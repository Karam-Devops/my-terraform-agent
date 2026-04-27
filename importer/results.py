# my-terraform-agent/importer/results.py
"""Structured return types for the importer workflow.

Why a dedicated module
----------------------
Phase 0 audit (punchlist CC-4) flagged that ``run_workflow()`` returned
``None`` on every path -- success, failure, user-cancel, "project has
no resources". Any caller that wanted to know whether to surface a
red banner had to either scrape stdout or run through the function
twice. Cloud Run operator dashboards had no structured signal to
alert on. This module fixes that by pinning a single result shape.

The A+D pattern
---------------
``run_workflow`` uses the Accept-or-Deliver pattern:

* **Accept** (raise): inputs/environment invalid -- the workflow
  cannot START. ``PreflightError`` in ``common.errors`` is the
  typed exception for this case. The ``__main__`` guard and the
  future Streamlit handler catch it and render ``.user_hint``.

* **Deliver** (return ``WorkflowResult``): the workflow ran to
  completion, regardless of per-resource outcomes. The result's
  ``.failed`` and ``.imported`` counts tell the caller what happened.
  ``.exit_code`` is 0 iff ``.failed == 0`` -- usable directly by
  the CLI entry-point and by Streamlit status indicators.

Design choices
--------------
* **frozen=True**: a result is a snapshot. Letting code mutate it
  after the fact defeats the point of having a structured return.
  Freezing also lets us hash/equality-compare results in tests.
* **No `success: bool`**: booleans lose information. ``failed == 0``
  is the source of truth; ``exit_code`` derives from it. A separate
  success flag would drift.
* **Counts, not lists of mappings**: the result is for LOGGING and
  UI SUMMARY. The full mapping objects carry tenant/project context
  that the result will be emitted into Cloud Logging -- we don't
  want to widen the log payload by embedding every resource's
  detailed mapping. Detailed per-resource status stays inside
  ``run_workflow`` locals; operators who need it run with higher log
  verbosity and read the per-resource ``subprocess_*`` events.
* **``as_fields()``**: a flat dict suitable for ``log.info(...,
  **result.as_fields())``. Pinning the field names here means
  dashboards and alert rules can filter on them safely.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True)
class WorkflowResult:
    """The outcome of a single importer ``run_workflow`` invocation.

    Attributes:
        project_id: The GCP project the workflow ran against. Empty
            string only if the workflow was aborted before project-ID
            resolution -- PreflightError should have been raised
            instead, so an empty ``project_id`` in a delivered result
            is a bug.
        selected: How many assets the operator (or future UI) chose
            to import. Zero when the user cancelled the selection
            menu or when discovery found no supported resources --
            both are workflow-complete cases, not errors.
        imported: Resources that reached green at the end of the
            workflow (HCL generated + ``terraform import`` succeeded
            + per-resource plan passed). ``imported + failed +
            skipped == selected`` always holds.
        failed: Resources that ended red: HCL generation failed,
            ``terraform import`` failed, or the final plan reported
            a diff the operator did not resolve. User-initiated
            "Skip resource" (menu option [3]) during interactive
            correction also lands here -- the operator chose to
            walk away from a failing resource.
        skipped: The reconciliation bucket -- any selected asset
            whose asset type had no Terraform mapping, or which
            dropped out of accounting before reaching the plan
            stage. Always equals ``selected - imported - failed``.
        duration_s: Total wall-clock seconds from workflow start to
            return. Captured with ``time.monotonic()`` so clock
            adjustments don't skew it.

    Exit code:
        ``exit_code`` is 0 iff ``failed == 0``. The ``__main__``
        guard and Streamlit status handler both read this directly
        rather than branching on individual counts -- keeps the
        "did the workflow succeed?" check in one place.
    """

    project_id: str
    selected: int
    imported: int
    failed: int
    skipped: int
    duration_s: float
    # CG-7 (P4 hotfix): resources whose .tf was quarantined after
    # exhausting auto-correct retries. Default 0 to preserve the
    # field set's back-compat for any caller constructing
    # WorkflowResult directly. The accounting invariant becomes:
    #   imported + needs_attention + failed + skipped == selected.
    # Customer-facing UI surfaces this as the "Needs Attention"
    # bucket per CC-5 spec; CLI just prints the count.
    needs_attention: int = 0

    @property
    def exit_code(self) -> int:
        """0 iff every selected resource reached green; 1 otherwise.

        Note: a workflow with selected=0 (nothing to do) returns 0.
        That's correct -- the workflow completed, the operator
        chose not to import anything, there's nothing to fail on.

        CG-7: needs_attention also flips exit_code to 1 -- a
        quarantined resource is a finding the operator must review,
        not a silent OK. Mirrors how DriftReport.exit_code treats
        ``unmanaged`` and ``inventory_errors`` as non-zero.
        """
        if self.failed > 0 or self.needs_attention > 0:
            return 1
        return 0

    def as_fields(self) -> dict[str, Any]:
        """Flat dict for structured-log emission.

        Usage::

            log.info("workflow_complete", **result.as_fields())

        Pinning the key names here (``project_id``, ``selected``,
        ``imported``, ``failed``, ``skipped``, ``duration_s``) means
        Cloud Logging dashboards and alert rules can filter on them
        reliably. Renaming any of these is a breaking change for
        ops -- do it with a visible punch-list entry.
        """
        return asdict(self)
