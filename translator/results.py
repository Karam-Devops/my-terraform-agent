# my-terraform-agent/translator/results.py
"""Structured return types for the translator's batch entry point.

Mirrors the importer's ``WorkflowResult`` (C3) shape and intent: callers
get a frozen dataclass with structured per-file outcomes + counts, not
a bag of booleans + paths.

CC-6 (Phase 1 retro punchlist) flagged the gap: the translator's
single-file `run_translation_pipeline(target_cloud, source)` returned
a `(bool, Optional[str])` tuple. SaaS UI (Phase 6) needs to render
"7 of 10 translated" with per-file detail; couldn't do that off the
single-file return shape. P3-6 fixes the backend half by adding
``run_translation_batch()`` that returns a ``TranslationResult``;
the Phase 6 UI consumes the same dataclass.

Why mirror WorkflowResult instead of inventing a new shape
----------------------------------------------------------
Operators reading per-engine reports in the same dashboard see two
different result shapes today: importer.WorkflowResult and (with this
commit) translator.TranslationResult. Keeping the field names + the
exit_code property + the as_fields() method consistent across engines
means operators can write one alert rule that works for both. The
field set differs slightly because the units are different (importer:
resources; translator: files), but the SHAPE is identical.

Same A+D return contract as importer:
  * Per-file failures (LLM exception, validator failure, file-write
    error) -> entry in `files` list with status="failed", workflow
    completes normally, batch result is RETURNED.
  * Inputs/environment failures (target_cloud invalid, workdir
    unreadable) -> raise PreflightError, no batch result returned.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, List, Literal, Optional


@dataclass(frozen=True)
class FileOutcome:
    """The outcome of translating one source file in a batch.

    Attributes:
        source_path: Absolute or relative path of the GCP source ``.tf``
            file fed to the translator.
        target_cloud: "aws" or "azure" -- same value across all files
            in the batch but repeated here so per-file outcomes are
            self-describing in the UI.
        status: One of:
            "translated"      - HCL generated AND passed validation
            "needs_attention" - HCL generated but failed validation
                (best-effort output saved; operator should review)
            "failed"          - file couldn't be read, LLM call failed
                permanently, or output couldn't be written
        output_path: Where the translated HCL was saved. None for
            ``status == "failed"`` (no output produced).
        validation_error: First line of the validator's error output
            when status is "needs_attention" / "failed". Empty string
            when status is "translated". Customer-friendly summary;
            full error preserved in the structured log.
        duration_s: Wall-clock seconds spent on this file alone
            (LLM round-trips, validation, write). Sum of file
            durations is approximately the batch duration, modulo
            sequential vs. parallel execution.
    """

    source_path: str
    target_cloud: str
    status: Literal["translated", "needs_attention", "failed"]
    output_path: Optional[str] = None
    validation_error: str = ""
    duration_s: float = 0.0


@dataclass(frozen=True)
class TranslationResult:
    """Outcome of a single ``run_translation_batch`` invocation.

    Counts derive from the per-file ``files`` list -- they're stored
    explicitly for fast dashboard rollups (Cloud Logging filters on
    `translated`/`failed`/`needs_attention` keys without needing to
    iterate the array each time).

    Attributes:
        target_cloud: "aws" or "azure".
        selected: How many source files the operator (or future UI)
            chose to translate.
        translated: Files that reached green (status="translated") --
            HCL written AND passed schema validation.
        needs_attention: Files where HCL was generated but failed
            validation (best-effort output saved). UI surfaces these
            as yellow "needs attention" cards per CC-5 contract.
        failed: Files that couldn't be processed at all (read error,
            LLM permanent failure, file-write error). UI surfaces
            these as red "couldn't process" cards.
        skipped: Reconciliation bucket -- selected files not accounted
            for elsewhere. Should always be zero in normal operation;
            non-zero indicates an accounting bug.
        duration_s: Total wall-clock from batch start to return.
        files: Per-file outcomes (FileOutcome list, ordered as the
            input list).
    """

    target_cloud: str
    selected: int
    translated: int
    needs_attention: int
    failed: int
    skipped: int
    duration_s: float
    files: List[FileOutcome] = field(default_factory=list)

    @property
    def exit_code(self) -> int:
        """0 iff every file reached "translated"; 1 if any file ended
        up in needs_attention or failed.

        Mirrors WorkflowResult.exit_code semantics. CI orchestrators
        wrapping the translator can treat any non-zero exit as
        "human review required".
        """
        return 0 if (self.failed == 0 and self.needs_attention == 0) else 1

    def as_fields(self) -> dict[str, Any]:
        """Flat dict for structured-log emission. Excludes the heavy
        ``files`` list -- per-file detail is logged via separate
        ``translation_file_outcome`` events during the batch run, not
        re-emitted here. Same convention as WorkflowResult.as_fields.
        """
        d = asdict(self)
        d.pop("files", None)
        return d
