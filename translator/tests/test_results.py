# translator/tests/test_results.py
"""P3-6 unit tests for TranslationResult + FileOutcome dataclasses.

Pure dataclass tests -- no engine, no LLM, no I/O. Pin the field
names + the exit_code semantics + the as_fields() shape, same way
importer/tests/test_results.py pins WorkflowResult.

These two files (importer's WorkflowResult and translator's
TranslationResult) deliberately mirror each other -- both inherit
the C3 A+D pattern. Future maintainer changing one without changing
the other risks silent diverence; the parallel test files highlight
the symmetry.
"""

from __future__ import annotations

import unittest
from dataclasses import FrozenInstanceError

from translator.results import FileOutcome, TranslationResult


class TranslationResultTests(unittest.TestCase):

    def _sample(self, **overrides):
        base = dict(
            target_cloud="aws",
            selected=10,
            translated=7,
            needs_attention=2,
            failed=1,
            skipped=0,
            duration_s=84.3,
            files=[],
        )
        base.update(overrides)
        return TranslationResult(**base)

    def test_carries_all_required_fields(self):
        r = self._sample()
        self.assertEqual(r.target_cloud, "aws")
        self.assertEqual(r.selected, 10)
        self.assertEqual(r.translated, 7)
        self.assertEqual(r.needs_attention, 2)
        self.assertEqual(r.failed, 1)
        self.assertEqual(r.skipped, 0)
        self.assertEqual(r.duration_s, 84.3)
        self.assertEqual(r.files, [])

    def test_exit_code_zero_when_all_translated(self):
        r = self._sample(translated=10, needs_attention=0, failed=0)
        self.assertEqual(r.exit_code, 0)

    def test_exit_code_nonzero_on_any_failure(self):
        r = self._sample(translated=9, needs_attention=0, failed=1)
        self.assertEqual(r.exit_code, 1)

    def test_exit_code_nonzero_on_needs_attention(self):
        """The whole point of the needs_attention bucket: it counts as
        non-zero exit even if no file outright failed -- the customer
        still has to review them, so CI should not green-light."""
        r = self._sample(translated=8, needs_attention=2, failed=0)
        self.assertEqual(r.exit_code, 1)

    def test_is_frozen(self):
        r = self._sample()
        with self.assertRaises(FrozenInstanceError):
            r.failed = 0  # type: ignore[misc]

    def test_as_fields_excludes_files_list(self):
        """`files` can be a long list (50+ entries on a big batch);
        log emission needs the counts, not the full per-file payload."""
        f = FileOutcome(
            source_path="/x/google_compute_instance_poc_vm.tf",
            target_cloud="aws",
            status="translated",
            output_path="/x/translated/aws/aws_translated_compute_instance_poc_vm.tf",
        )
        r = self._sample(files=[f, f, f])
        d = r.as_fields()
        self.assertNotIn("files", d)
        # Counts still present so the log line is useful.
        self.assertEqual(d["translated"], 7)
        self.assertEqual(d["selected"], 10)


class FileOutcomeTests(unittest.TestCase):

    def test_minimal_translated_outcome(self):
        f = FileOutcome(
            source_path="src.tf",
            target_cloud="aws",
            status="translated",
            output_path="out.tf",
        )
        self.assertEqual(f.status, "translated")
        self.assertEqual(f.validation_error, "")
        self.assertEqual(f.duration_s, 0.0)

    def test_failed_outcome_has_no_output_path(self):
        f = FileOutcome(
            source_path="src.tf",
            target_cloud="aws",
            status="failed",
            output_path=None,
            validation_error="LLM permanent failure",
            duration_s=12.5,
        )
        self.assertIsNone(f.output_path)
        self.assertEqual(f.validation_error, "LLM permanent failure")

    def test_needs_attention_keeps_output_path(self):
        """Pipeline saves best-effort HCL even on validation failure;
        FileOutcome reflects that the file IS on disk for review."""
        f = FileOutcome(
            source_path="src.tf",
            target_cloud="aws",
            status="needs_attention",
            output_path="out.tf",
            validation_error="schema validation failed",
        )
        self.assertEqual(f.output_path, "out.tf")
        self.assertEqual(f.status, "needs_attention")

    def test_is_frozen(self):
        f = FileOutcome(
            source_path="src.tf",
            target_cloud="aws",
            status="translated",
            output_path="out.tf",
        )
        with self.assertRaises(FrozenInstanceError):
            f.status = "failed"  # type: ignore[misc]


if __name__ == "__main__":
    unittest.main()
