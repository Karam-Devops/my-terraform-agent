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
        """
        r = self._sample()
        fields = r.as_fields()
        self.assertEqual(
            set(fields.keys()),
            {"project_id", "selected", "imported", "failed",
             "skipped", "duration_s"},
        )
        self.assertEqual(fields["project_id"], "poc-sa-dev")
        self.assertEqual(fields["selected"], 10)
        self.assertEqual(fields["duration_s"], 42.5)

    def test_equality_by_value(self):
        """Two results with the same counts compare equal — useful in tests."""
        a = self._sample()
        b = self._sample()
        self.assertEqual(a, b)
        self.assertNotEqual(a, self._sample(failed=99))


if __name__ == "__main__":
    unittest.main()
