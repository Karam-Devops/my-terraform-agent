# importer/tests/test_results.py
"""Unit tests for ``importer.results.WorkflowResult``.

Coverage focus: the dataclass contract callers depend on --
field names (dashboards filter on them), ``exit_code`` derivation
(__main__ guard and Streamlit use it directly), and ``as_fields()``
shape (structured-log payload).

Why frozen matters: any code that mutates the result after it's
returned from ``run_workflow`` is circumventing the A+D contract.
Freezing lets us catch that at test time rather than in production
where a subtle ``result.failed = 0`` would silently hide red status.
"""

from __future__ import annotations

import unittest
from dataclasses import FrozenInstanceError

from importer.results import WorkflowResult


class WorkflowResultTests(unittest.TestCase):

    def _sample(self, **overrides):
        """Build a baseline result with field overrides -- keeps each
        test focused on the field it cares about."""
        base = dict(
            project_id="poc-sa-dev",
            selected=10,
            imported=8,
            failed=1,
            skipped=1,
            duration_s=42.5,
        )
        base.update(overrides)
        return WorkflowResult(**base)

    def test_carries_all_required_fields(self):
        """Every field in the contract must be accessible by name.

        Renaming any of these breaks dashboard queries and Streamlit
        rendering. Pin the exact names.
        """
        r = self._sample()
        self.assertEqual(r.project_id, "poc-sa-dev")
        self.assertEqual(r.selected, 10)
        self.assertEqual(r.imported, 8)
        self.assertEqual(r.failed, 1)
        self.assertEqual(r.skipped, 1)
        self.assertEqual(r.duration_s, 42.5)

    def test_exit_code_zero_when_no_failures(self):
        """Green workflow -> exit 0 (CLI success, Streamlit green banner)."""
        r = self._sample(failed=0, imported=10, skipped=0)
        self.assertEqual(r.exit_code, 0)

    def test_exit_code_nonzero_when_any_failure(self):
        """Red workflow -> exit 1, even if most resources succeeded.

        CI pipelines that wrap the importer must fail-closed on any
        resource failure -- partial success isn't success.
        """
        r = self._sample(failed=1, imported=9, skipped=0)
        self.assertEqual(r.exit_code, 1)

    def test_exit_code_zero_when_nothing_selected(self):
        """Operator cancelled selection / project has no resources.

        Not an error state — the workflow ran, there was nothing to do.
        Exit 0 so orchestrators don't alert on "nothing to import".
        """
        r = self._sample(selected=0, imported=0, failed=0, skipped=0)
        self.assertEqual(r.exit_code, 0)

    def test_is_frozen(self):
        """Mutating a delivered result is a bug — freeze enforces it."""
        r = self._sample()
        with self.assertRaises(FrozenInstanceError):
            r.failed = 0  # type: ignore[misc]

    def test_as_fields_shape(self):
        """``as_fields()`` is the log-emission payload shape.

        If a key is added/removed/renamed here, every Cloud Logging
        query keyed off ``jsonPayload.<field>`` breaks silently.

        CG-7 (P4 hotfix) added ``needs_attention`` as the 7th key --
        dashboards filtering on the previous 6 keys still work
        unchanged; new dashboards filtering on quarantine activity
        use the new key.
        """
        r = self._sample()
        fields = r.as_fields()
        self.assertEqual(
            set(fields.keys()),
            {"project_id", "selected", "imported", "failed",
             "skipped", "duration_s", "needs_attention"},
        )
        self.assertEqual(fields["project_id"], "poc-sa-dev")
        self.assertEqual(fields["selected"], 10)
        self.assertEqual(fields["duration_s"], 42.5)
        # New field defaults to 0 when caller doesn't pass it
        # (back-compat for the existing CLI flow).
        self.assertEqual(fields["needs_attention"], 0)

    def test_equality_by_value(self):
        """Two results with the same counts compare equal — useful in tests."""
        a = self._sample()
        b = self._sample()
        self.assertEqual(a, b)
        self.assertNotEqual(a, self._sample(failed=99))


class WorkflowResultNeedsAttentionTests(unittest.TestCase):
    """CG-7 (P4 hotfix): the new ``needs_attention`` field tracks
    quarantined resources -- the customer-facing 'Needs Attention'
    bucket per CC-5."""

    def _sample(self, **overrides):
        base = dict(
            project_id="dev-proj-470211",
            selected=16,
            imported=13,
            failed=1,
            skipped=0,
            duration_s=42.0,
            needs_attention=2,
        )
        base.update(overrides)
        return WorkflowResult(**base)

    def test_needs_attention_field_carries_count(self):
        r = self._sample()
        self.assertEqual(r.needs_attention, 2)

    def test_needs_attention_defaults_to_zero(self):
        # Back-compat: existing callers that don't pass needs_attention
        # get the same 6-field shape they always had.
        r = WorkflowResult(
            project_id="x", selected=5, imported=5,
            failed=0, skipped=0, duration_s=1.0,
        )
        self.assertEqual(r.needs_attention, 0)

    def test_exit_code_nonzero_on_needs_attention_alone(self):
        # Quarantined resources require operator review; exit 1 forces
        # CI pipelines to gate, even when failed=0.
        r = self._sample(failed=0, needs_attention=1)
        self.assertEqual(r.exit_code, 1)

    def test_exit_code_zero_when_clean(self):
        r = self._sample(failed=0, needs_attention=0)
        self.assertEqual(r.exit_code, 0)


if __name__ == "__main__":
    unittest.main()
