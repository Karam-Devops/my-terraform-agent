# detector/tests/test_remediator_hygiene.py
"""P4-1 hygiene tests for detector.remediator.

Covers:
  * `_state_path()` raises PreflightError on missing workdir (no silent
    cwd fallback — see CC-2 detector half + P4-1 commit for rationale).
  * `_TERRAFORM_TIMEOUTS` map covers every subcommand we invoke.
  * `_start_kill_watchdog()` returns a started daemon thread (the kill
    behavior itself is exercised in the full-engine SMOKE).

These tests deliberately don't shell out to a real terraform binary.
The watchdog *behavior* (kill after timeout) is verified in P4-10 SMOKE.
"""

from __future__ import annotations

import importlib.util
import os
import sys
import threading
import types
import unittest
from unittest.mock import MagicMock

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))


def _load_remediator():
    """Same loader as test_remediator_headless.py -- stub detector
    parent + load real submodules + load remediator."""
    if "detector.remediator" in sys.modules:
        return sys.modules["detector.remediator"]

    if "detector" not in sys.modules:
        pkg = types.ModuleType("detector")
        pkg.__path__ = [os.path.join(PROJECT_ROOT, "detector")]
        sys.modules["detector"] = pkg

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


class StatePathHardErrorTests(unittest.TestCase):
    """`_state_path()` MUST NOT silently fall back to os.getcwd() when
    workdir is missing. The previous behavior was the exact silent-cwd
    pattern that caused the per-project workdir refactor; under
    concurrency it would risk wrong-tenant state reads."""

    def setUp(self):
        self.r = _load_remediator()
        # Lazy-load PreflightError the same way remediator.py does.
        from common.errors import PreflightError
        self.PreflightError = PreflightError

    def test_none_workdir_raises_preflight_error(self):
        with self.assertRaises(self.PreflightError) as ctx:
            self.r._state_path(workdir=None)
        # Stage tag is queryable in structured logs / dashboards.
        self.assertEqual(ctx.exception.stage, "resolve_workdir")

    def test_empty_string_workdir_raises_preflight_error(self):
        # Empty string is also a "missing" value under our truthiness
        # check -- defends against a programmatic caller passing ""
        # accidentally.
        with self.assertRaises(self.PreflightError):
            self.r._state_path(workdir="")

    def test_no_arg_call_raises_preflight_error(self):
        # Default-arg path: caller forgot workdir kwarg entirely.
        with self.assertRaises(self.PreflightError):
            self.r._state_path()

    def test_explicit_workdir_returns_state_path_under_it(self):
        # Happy path: workdir provided -> returns workdir/STATE_FILE_NAME.
        from detector import config as det_cfg
        result = self.r._state_path(workdir="/some/abs/path")
        self.assertEqual(
            result,
            os.path.join("/some/abs/path", det_cfg.STATE_FILE_NAME),
        )


class TerraformTimeoutMapTests(unittest.TestCase):
    """The CC-2 detector half: every terraform subcommand we invoke
    must have a timeout budget, and the budgets must be sensible
    (per the punchlist's recommended values)."""

    def setUp(self):
        self.r = _load_remediator()

    def test_init_has_long_timeout_for_provider_download(self):
        # init downloads providers; first run is slow. Punchlist says 600s.
        self.assertEqual(self.r._TERRAFORM_TIMEOUTS["init"], 600)

    def test_plan_apply_have_per_target_timeouts(self):
        self.assertEqual(self.r._TERRAFORM_TIMEOUTS["plan"], 300)
        self.assertEqual(self.r._TERRAFORM_TIMEOUTS["apply"], 600)

    def test_import_has_per_resource_timeout(self):
        self.assertEqual(self.r._TERRAFORM_TIMEOUTS["import"], 120)

    def test_state_subcommands_have_short_timeout(self):
        # state rm / state mv / state list are in-memory ops; should
        # complete in seconds.
        self.assertEqual(self.r._TERRAFORM_TIMEOUTS["state"], 60)

    def test_default_timeout_falls_back_to_300s(self):
        # Any subcommand not in the explicit map gets the default budget.
        self.assertEqual(self.r._TERRAFORM_DEFAULT_TIMEOUT, 300)

    def test_every_invoked_subcommand_has_a_budget(self):
        # We invoke these subcommands across the file; every one must
        # have a budget so a hung process can never wedge the request.
        # Currently invoked: init, plan, apply, refresh, import, state.
        invoked = {"init", "plan", "apply", "refresh", "import", "state"}
        for op in invoked:
            with self.subTest(op=op):
                self.assertIn(
                    op, self.r._TERRAFORM_TIMEOUTS,
                    f"Subcommand `{op}` invoked without a timeout budget",
                )


class WatchdogWiringTests(unittest.TestCase):
    """Verifies the watchdog thread is a started daemon. Doesn't verify
    the kill itself (would need a real subprocess); that's covered in
    the full-engine SMOKE."""

    def setUp(self):
        self.r = _load_remediator()

    def test_watchdog_returns_a_started_daemon_thread(self):
        # Mock proc whose poll() always returns None (process never
        # exits). The watchdog will sleep and try to kill it. Since
        # kill() is also mocked, no actual process is harmed.
        proc = MagicMock()
        proc.poll.return_value = None  # still running, forever
        proc.kill = MagicMock()

        # Use a short timeout so we don't slow down the test suite.
        # We're not waiting for the kill to fire here; we just verify
        # the thread is alive and a daemon.
        t = self.r._start_kill_watchdog(proc, timeout_s=10)

        self.assertIsInstance(t, threading.Thread)
        self.assertTrue(t.daemon,
                        "Watchdog must be a daemon thread so it doesn't "
                        "outlive the host process")
        self.assertTrue(t.is_alive(),
                        "Watchdog must already be started when returned")
        self.assertEqual(t.name, "tf_kill_watchdog")


if __name__ == "__main__":
    unittest.main()
