# detector/tests/test_remediator_headless.py
"""
Smoke tests for the TODO #10 headless-mode refactor.

We stub `_run_terraform`, `_backup_state`, and `_reverify` so the tests
don't need a live terraform binary or a real cloud project. The point is
to exercise:
  1. ConfirmationPolicy plumbing — Auto vs Interactive both reach the
     action helpers and the helpers honour their answers.
  2. `remediate_one()` returns RemediationResult objects with the right
     `success`/`status`, including the invalid-action and exception paths.
  3. `run_remediation()` keeps its tty bail-out for the no-policy CLI
     path, and now skips the bail-out when an explicit policy is passed.
"""

from __future__ import annotations

import importlib.util
import os
import sys
import types
import unittest
from unittest.mock import patch

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))

# P4-1: _state_path() now hard-errors when workdir is missing (no silent
# cwd fallback under concurrency). Tests that exercise the apply / state-rm
# code paths (which reach _state_path for backup) MUST pass an explicit
# workdir. Used as a sentinel -- the actual filesystem path doesn't matter
# because _backup_state is mocked, but the value must be truthy.
_TEST_WORKDIR = "/test/workdir"


def _load_remediator():
    """Load detector.remediator without dragging in detector/__init__.py
    side-effects. We only need the module under test."""
    if "detector.remediator" in sys.modules:
        return sys.modules["detector.remediator"]
    # Stub the parent package shell so relative imports inside remediator.py
    # resolve. config / state_reader / cloud_snapshot / diff_engine are
    # imported by name; we provide minimal stand-ins.
    if "detector" not in sys.modules:
        pkg = types.ModuleType("detector")
        pkg.__path__ = [os.path.join(PROJECT_ROOT, "detector")]
        sys.modules["detector"] = pkg

    # Real submodule loads — diff_engine for ResourceDrift, others for
    # _state_path / reverify. They have no heavy deps so importing them
    # for real is cheaper than maintaining stubs.
    for name in ("config", "state_reader", "cloud_snapshot", "diff_engine"):
        if f"detector.{name}" not in sys.modules:
            spec = importlib.util.spec_from_file_location(
                f"detector.{name}",
                os.path.join(PROJECT_ROOT, "detector", f"{name}.py"),
            )
            mod = importlib.util.module_from_spec(spec)
            sys.modules[f"detector.{name}"] = mod
            spec.loader.exec_module(mod)

    spec = importlib.util.spec_from_file_location(
        "detector.remediator",
        os.path.join(PROJECT_ROOT, "detector", "remediator.py"),
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["detector.remediator"] = mod
    spec.loader.exec_module(mod)
    return mod


class ConfirmationPolicyTests(unittest.TestCase):
    def setUp(self):
        self.r = _load_remediator()

    def test_auto_policy_yes_no_returns_configured_answer(self):
        p = self.r.AutoConfirmPolicy()
        self.assertEqual(p.yes_no("anything", default="N"), "Y")

    def test_auto_policy_can_be_configured_to_say_no(self):
        p = self.r.AutoConfirmPolicy(answer="N")
        self.assertEqual(p.yes_no("anything", default="Y"), "N")

    def test_auto_policy_typed_always_passes(self):
        p = self.r.AutoConfirmPolicy()
        self.assertTrue(p.typed("expected_addr", "type it: "))

    def test_interactive_policy_delegates_to_input(self):
        p = self.r.InteractivePolicy()
        with patch("builtins.input", return_value="Y"):
            self.assertEqual(p.yes_no("Proceed? ", default="N"), "Y")
        with patch("builtins.input", return_value="resource.foo"):
            self.assertTrue(p.typed("resource.foo", "type it: "))
        with patch("builtins.input", return_value="wrong"):
            self.assertFalse(p.typed("resource.foo", "type it: "))


class RemediateOneTests(unittest.TestCase):
    def setUp(self):
        self.r = _load_remediator()

    def test_invalid_action_returns_structured_error(self):
        result = self.r.remediate_one("addr.x", "bogus_action")
        self.assertFalse(result.success)
        self.assertEqual(result.status, "invalid_action")
        self.assertIn("bogus_action", result.message)

    def test_restore_happy_path_with_auto_confirm(self):
        # Stub the terraform/state interactions: plan returns 2 (changes),
        # apply returns 0, backup succeeds, reverify reports clean.
        with patch.object(self.r, "_run_terraform") as mock_tf, \
             patch.object(self.r, "_backup_state", return_value="/tmp/backup"), \
             patch.object(self.r, "_reverify", return_value=None):
            mock_tf.side_effect = [2, 0]  # plan -> changes pending, apply -> ok
            result = self.r.remediate_one("foo.bar", "restore", auto_confirm=True,
                                          workdir=_TEST_WORKDIR)
        self.assertTrue(result.success, result.message)
        self.assertEqual(result.status, "ok")
        # Both plan and apply were attempted (auto-confirm did NOT skip
        # the apply gate).
        self.assertEqual(mock_tf.call_count, 2)
        plan_args = mock_tf.call_args_list[0][0][0]
        apply_args = mock_tf.call_args_list[1][0][0]
        self.assertEqual(plan_args[0], "plan")
        self.assertEqual(apply_args[0], "apply")

    def test_restore_user_decline_via_interactive_policy(self):
        # InteractivePolicy + input() returning N should skip the apply.
        with patch.object(self.r, "_run_terraform") as mock_tf, \
             patch.object(self.r, "_backup_state", return_value="/tmp/backup"), \
             patch.object(self.r, "_reverify", return_value=None), \
             patch("builtins.input", return_value="N"):
            mock_tf.side_effect = [2]  # only plan; apply must not run
            result = self.r.remediate_one("foo.bar", "restore", auto_confirm=False)
        self.assertFalse(result.success)
        self.assertEqual(result.status, "failed")
        self.assertEqual(mock_tf.call_count, 1)
        self.assertEqual(mock_tf.call_args_list[0][0][0][0], "plan")

    def test_restore_no_op_plan_returns_failed(self):
        # Plan exit code 0 means terraform sees nothing to change. We
        # surface this as failed (it's not "ok" — Restore was a no-op
        # and the operator's expectation went unmet).
        with patch.object(self.r, "_run_terraform", return_value=0), \
             patch.object(self.r, "_backup_state", return_value="/tmp/backup"):
            result = self.r.remediate_one("foo.bar", "restore", auto_confirm=True)
        self.assertFalse(result.success)
        self.assertEqual(result.status, "failed")

    def test_drop_with_auto_confirm_skips_typed_gate(self):
        # `_drop` normally requires you to type the address back. With
        # AutoConfirmPolicy.typed() returning True, it should proceed
        # straight to `terraform state rm`.
        with patch.object(self.r, "_run_terraform", return_value=0) as mock_tf, \
             patch.object(self.r, "_backup_state", return_value="/tmp/backup"):
            result = self.r.remediate_one("foo.bar", "drop", auto_confirm=True,
                                          workdir=_TEST_WORKDIR)
        self.assertTrue(result.success, result.message)
        self.assertEqual(result.status, "ok")
        # Should have called `terraform state rm foo.bar` exactly once.
        mock_tf.assert_called_once()
        called_args = mock_tf.call_args[0][0]
        self.assertEqual(called_args[:2], ["state", "rm"])
        self.assertEqual(called_args[2], "foo.bar")

    def test_drop_interactive_with_wrong_typed_address_aborts(self):
        with patch.object(self.r, "_run_terraform") as mock_tf, \
             patch.object(self.r, "_backup_state", return_value="/tmp/backup"), \
             patch("builtins.input", return_value="WRONG_ADDRESS"):
            result = self.r.remediate_one("foo.bar", "drop", auto_confirm=False)
        self.assertFalse(result.success)
        self.assertEqual(result.status, "failed")
        # No terraform call should have been made — typed-gate aborted first.
        mock_tf.assert_not_called()

    def test_exception_in_handler_is_caught(self):
        with patch.object(self.r, "_run_terraform", side_effect=RuntimeError("boom")), \
             patch.object(self.r, "_backup_state", return_value="/tmp/backup"):
            result = self.r.remediate_one("foo.bar", "restore", auto_confirm=True)
        self.assertFalse(result.success)
        self.assertEqual(result.status, "exception")
        self.assertIn("RuntimeError", result.message)
        self.assertIn("boom", result.message)


class RunRemediationTests(unittest.TestCase):
    def setUp(self):
        self.r = _load_remediator()
        # Build one drifted resource to feed the loop. Using ResourceDrift
        # from diff_engine keeps the dataclass shape correct without us
        # having to mirror its fields.
        from detector.diff_engine import ResourceDrift
        self.drift = ResourceDrift(
            tf_address="foo.bar",
            tf_type="google_storage_bucket",
            items=[],
            error=None,
        )
        # has_drift defaults to False on an empty items list, so we patch
        # the property at the instance level via a flag the dataclass
        # supports — or we forge it. Easier: pick a drift with a synthesised
        # item if the dataclass requires it.
        if not self.drift.has_drift:
            # Inject a fake item so .has_drift becomes True. The exact
            # shape doesn't matter for these tests — we only need the
            # truthiness.
            from detector.diff_engine import DriftItem
            self.drift.items = [DriftItem(op="changed", path="x", state_value=1, cloud_value=2)]
        self.assertTrue(self.drift.has_drift)

    def test_no_policy_no_tty_bails_out_silently(self):
        # No explicit policy + non-interactive stdin = legacy bail-out.
        with patch.object(self.r, "_is_interactive", return_value=False):
            summary = self.r.run_remediation([self.drift])
        self.assertEqual(summary.restored, [])
        self.assertEqual(summary.accepted, [])
        self.assertEqual(summary.skipped, [])
        self.assertEqual(summary.failed, [])

    def test_explicit_policy_bypasses_tty_bail_out(self):
        # When the caller supplies a policy, we trust them — no tty check.
        # The caller's policy then drives the action prompts. Here we
        # answer "Y" to the walk-through gate, "S" to skip the action,
        # so the loop doesn't try to talk to terraform.
        with patch.object(self.r, "_is_interactive", return_value=False), \
             patch.object(self.r, "_prompt") as mock_prompt:
            # Order: walk-through Y/N prompt, then per-resource action picker.
            mock_prompt.side_effect = ["Y", "S"]
            summary = self.r.run_remediation(
                [self.drift],
                confirmation=self.r.AutoConfirmPolicy(),
            )
        self.assertEqual(summary.skipped, ["foo.bar"])

    def test_cli_path_wires_policy_gate_into_restore(self):
        # Regression test for the CLI-vs-programmatic gate-wiring gap caught
        # in the live drift test. The fix added enable_policy_gate +
        # _policy_check_for(...) plumbing into run_remediation() so the
        # interactive walkthrough also gets the gate, not just remediate_one().
        #
        # We patch _policy_check_for to return a closure that yields a HIGH
        # impact, then drive the walkthrough: walk-through Y, action [R],
        # which should fire the gate. We answer N to the gate-override
        # prompt, expecting `terraform apply` to never be called.
        from unittest.mock import MagicMock

        # Stub: factory returns a closure that returns a fake HIGH impact.
        class _Imp:
            is_violating = True
            high_count = 1
            med_count = 0
            low_count = 0
            violations = []  # rendered list; empty is fine for this test
        fake_check = MagicMock(return_value=_Imp())
        fake_factory = MagicMock(return_value=fake_check)

        # AutoConfirmPolicy(answer="N") makes the override prompt say no.
        # _prompt drives the multi-choice action picker (Y to walk, R to restore).
        with patch.object(self.r, "_is_interactive", return_value=False), \
             patch.object(self.r, "_policy_check_for", fake_factory), \
             patch.object(self.r, "_run_terraform") as mock_tf, \
             patch.object(self.r, "_backup_state") as mock_backup, \
             patch.object(self.r, "_reverify", return_value=None), \
             patch.object(self.r, "_prompt") as mock_prompt:
            mock_prompt.side_effect = ["Y", "R"]
            mock_tf.side_effect = [2]  # plan returns "changes"; apply must NOT run
            summary = self.r.run_remediation(
                [self.drift],
                confirmation=self.r.AutoConfirmPolicy(answer="N"),
            )

        # Gate factory was called for the resource the user picked Restore on.
        # The workdir kwarg defaults to None when run_remediation is called
        # without an explicit workdir (CLI back-compat path).
        fake_factory.assert_called_once_with("foo.bar", workdir=None)
        # Gate evaluated the impact (closure invoked).
        fake_check.assert_called_once()
        # Plan ran; apply did NOT (gate blocked); backup never ran.
        self.assertEqual(mock_tf.call_count, 1)
        self.assertEqual(mock_tf.call_args_list[0][0][0][0], "plan")
        mock_backup.assert_not_called()
        # Resource reported as failed (gate refused).
        self.assertEqual(summary.failed, [("foo.bar", "restore")])
        self.assertEqual(summary.restored, [])

    def test_cli_path_skips_gate_when_disabled(self):
        # enable_policy_gate=False short-circuits the gate path entirely.
        # The factory should NEVER be called — proving the kwarg flows
        # through to the loop's gate-build branch.
        from unittest.mock import MagicMock
        fake_factory = MagicMock()

        with patch.object(self.r, "_is_interactive", return_value=False), \
             patch.object(self.r, "_policy_check_for", fake_factory), \
             patch.object(self.r, "_run_terraform") as mock_tf, \
             patch.object(self.r, "_backup_state", return_value="/tmp/backup"), \
             patch.object(self.r, "_reverify", return_value=None), \
             patch.object(self.r, "_prompt") as mock_prompt:
            mock_prompt.side_effect = ["Y", "R"]
            mock_tf.side_effect = [2, 0]  # plan + apply both run
            summary = self.r.run_remediation(
                [self.drift],
                confirmation=self.r.AutoConfirmPolicy(answer="Y"),
                enable_policy_gate=False,
                workdir=_TEST_WORKDIR,
            )

        fake_factory.assert_not_called()
        self.assertEqual(summary.restored, ["foo.bar"])


# --- TODO #14: pre-apply policy gate -----------------------------------
#
# We inject a `policy_check` closure directly to avoid spinning up
# conftest / loading real Rego. The closure returns a fake PolicyImpact
# with whatever violation counts each test needs. This isolates the gate
# logic (threshold math, override prompt, fail-open) from the policy
# evaluation pipeline, which has its own tests.

class _FakeViolation:
    """Mirrors engine.Violation just enough for the gate's render path."""
    def __init__(self, severity: str, rule_id: str = "TEST_RULE", message: str = "x"):
        self.severity = severity
        self.rule_id = rule_id
        self.message = message


class _FakeImpact:
    """Mirrors policy.integration.PolicyImpact's read surface."""
    def __init__(self, severities):
        self.violations = [_FakeViolation(s) for s in severities]

    @property
    def is_violating(self):
        return bool(self.violations)

    @property
    def high_count(self):
        return sum(1 for v in self.violations if v.severity == "HIGH")

    @property
    def med_count(self):
        return sum(1 for v in self.violations if v.severity == "MED")

    @property
    def low_count(self):
        return sum(1 for v in self.violations if v.severity == "LOW")


class PolicyGateTests(unittest.TestCase):
    def setUp(self):
        self.r = _load_remediator()

    # --- _run_policy_gate unit shape ---

    def test_gate_no_check_proceeds(self):
        # No policy_check supplied = gate is a no-op.
        ok = self.r._run_policy_gate("foo.bar", None, self.r.AutoConfirmPolicy())
        self.assertTrue(ok)

    def test_gate_no_violations_proceeds(self):
        check = lambda: _FakeImpact([])
        ok = self.r._run_policy_gate("foo.bar", check, self.r.AutoConfirmPolicy())
        self.assertTrue(ok)

    def test_gate_med_only_below_default_threshold_proceeds(self):
        # block_at default is "HIGH" — a MED-only impact prints but doesn't block.
        check = lambda: _FakeImpact(["MED", "MED", "LOW"])
        ok = self.r._run_policy_gate(
            "foo.bar", check,
            self.r.AutoConfirmPolicy(answer="N"),  # would block if threshold counted MED
        )
        self.assertTrue(ok)

    def test_gate_high_blocks_when_user_says_no(self):
        check = lambda: _FakeImpact(["HIGH"])
        ok = self.r._run_policy_gate(
            "foo.bar", check, self.r.AutoConfirmPolicy(answer="N"),
        )
        self.assertFalse(ok)

    def test_gate_high_proceeds_when_user_overrides_yes(self):
        # AutoConfirmPolicy default answer is "Y", which simulates an
        # operator clicking "Override and apply anyway".
        check = lambda: _FakeImpact(["HIGH", "HIGH"])
        ok = self.r._run_policy_gate(
            "foo.bar", check, self.r.AutoConfirmPolicy(answer="Y"),
        )
        self.assertTrue(ok)

    def test_gate_block_at_med_blocks_on_med(self):
        # Custom threshold: the gate's block_at param raises sensitivity.
        check = lambda: _FakeImpact(["MED"])
        ok = self.r._run_policy_gate(
            "foo.bar", check,
            self.r.AutoConfirmPolicy(answer="N"),
            block_at="MED",
        )
        self.assertFalse(ok)

    def test_gate_eval_failure_falls_open(self):
        # Closure raises -> gate returns True (fail-open) so we never
        # accidentally block a legitimate apply on a policy-layer bug.
        # The implementation guards inside _policy_check_for; here we
        # just confirm an impact-of-None proceeds.
        ok = self.r._run_policy_gate(
            "foo.bar", lambda: None, self.r.AutoConfirmPolicy(answer="N"),
        )
        self.assertTrue(ok)

    # --- gate wired into _restore via remediate_one ---

    def test_restore_blocked_by_gate_skips_apply(self):
        # HIGH violation + N answer must block apply BEFORE backup runs.
        # We assert no apply call AND no backup call to prove ordering.
        with patch.object(self.r, "_run_terraform") as mock_tf, \
             patch.object(self.r, "_backup_state") as mock_backup, \
             patch.object(self.r, "_reverify", return_value=None):
            mock_tf.side_effect = [2]  # plan only — apply must NOT run
            result = self.r.remediate_one(
                "foo.bar", "restore",
                confirmation=self.r.AutoConfirmPolicy(answer="N"),
                policy_check=lambda: _FakeImpact(["HIGH"]),
            )
        self.assertFalse(result.success)
        self.assertEqual(result.status, "failed")
        self.assertEqual(mock_tf.call_count, 1)
        self.assertEqual(mock_tf.call_args_list[0][0][0][0], "plan")
        mock_backup.assert_not_called()  # gate fires before backup

    def test_restore_proceeds_when_gate_overridden(self):
        # HIGH violation but operator answers Y — apply proceeds.
        with patch.object(self.r, "_run_terraform") as mock_tf, \
             patch.object(self.r, "_backup_state", return_value="/tmp/backup"), \
             patch.object(self.r, "_reverify", return_value=None):
            mock_tf.side_effect = [2, 0]  # plan -> changes, apply -> ok
            result = self.r.remediate_one(
                "foo.bar", "restore",
                confirmation=self.r.AutoConfirmPolicy(answer="Y"),
                policy_check=lambda: _FakeImpact(["HIGH"]),
                workdir=_TEST_WORKDIR,
            )
        self.assertTrue(result.success, result.message)
        self.assertEqual(mock_tf.call_count, 2)

    def test_restore_with_no_violations_proceeds_normally(self):
        with patch.object(self.r, "_run_terraform") as mock_tf, \
             patch.object(self.r, "_backup_state", return_value="/tmp/backup"), \
             patch.object(self.r, "_reverify", return_value=None):
            mock_tf.side_effect = [2, 0]
            result = self.r.remediate_one(
                "foo.bar", "restore",
                auto_confirm=True,
                policy_check=lambda: _FakeImpact([]),
                workdir=_TEST_WORKDIR,
            )
        self.assertTrue(result.success)
        self.assertEqual(mock_tf.call_count, 2)

    def test_enable_policy_gate_false_skips_gate(self):
        # Even with a HIGH-violation closure, enable_policy_gate=False
        # must skip the gate (closure should not even be called).
        # We prove "not called" by handing a closure that would raise.
        def boom():
            raise AssertionError("policy_check was called despite enable_policy_gate=False")
        with patch.object(self.r, "_run_terraform") as mock_tf, \
             patch.object(self.r, "_backup_state", return_value="/tmp/backup"), \
             patch.object(self.r, "_reverify", return_value=None):
            mock_tf.side_effect = [2, 0]
            result = self.r.remediate_one(
                "foo.bar", "restore",
                auto_confirm=True,
                enable_policy_gate=False,
                policy_check=boom,
                workdir=_TEST_WORKDIR,
            )
        self.assertTrue(result.success)

    def test_recreate_blocked_by_gate(self):
        # Same gate, different action helper. _recreate skips terraform
        # plan entirely — gate fires after typed-confirm and before backup.
        with patch.object(self.r, "_run_terraform") as mock_tf, \
             patch.object(self.r, "_backup_state") as mock_backup:
            result = self.r.remediate_one(
                "foo.bar", "recreate",
                confirmation=self.r.AutoConfirmPolicy(answer="N"),
                policy_check=lambda: _FakeImpact(["HIGH"]),
            )
        self.assertFalse(result.success)
        self.assertEqual(result.status, "failed")
        mock_tf.assert_not_called()
        mock_backup.assert_not_called()

    def test_accept_does_not_get_gate_kwarg(self):
        # Accept never mutates cloud, so the gate is not wired in. The
        # test would crash with TypeError if remediate_one mistakenly
        # forwarded policy_check= to _accept.
        with patch.object(self.r, "_run_terraform") as mock_tf, \
             patch.object(self.r, "_backup_state", return_value="/tmp/backup"), \
             patch.object(self.r, "_reverify", return_value=None):
            mock_tf.side_effect = [2, 0]
            result = self.r.remediate_one(
                "foo.bar", "accept",
                auto_confirm=True,
                # Passing policy_check should be silently ignored for accept.
                policy_check=lambda: _FakeImpact(["HIGH"]),
                workdir=_TEST_WORKDIR,
            )
        self.assertTrue(result.success, result.message)

    def test_drop_does_not_get_gate_kwarg(self):
        # Same as accept — Drop only mutates state, no gate.
        with patch.object(self.r, "_run_terraform", return_value=0), \
             patch.object(self.r, "_backup_state", return_value="/tmp/backup"):
            result = self.r.remediate_one(
                "foo.bar", "drop",
                auto_confirm=True,
                policy_check=lambda: _FakeImpact(["HIGH"]),
                workdir=_TEST_WORKDIR,
            )
        self.assertTrue(result.success, result.message)


if __name__ == "__main__":
    unittest.main()
