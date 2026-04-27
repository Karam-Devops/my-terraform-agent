# policy/tests/test_violation_caps.py
"""P4-1 tests for the policy violation caps.

Two layers of defense per the punchlist (CC-2 policy half):
  * MAX_VIOLATIONS_PER_CALL (engine.evaluate truncates per-resource)
  * MAX_VIOLATIONS_PER_RUN  (run.py CLI truncates aggregate)

Defends against:
  - Buggy rule iterating a long list (one resource produces 1000s of
    violations).
  - Malicious .tf with 10k trivial resources blowing up output volume.

These tests verify the CONSTANTS exist + are sensible. The truncation
behavior on real input is exercised by the full-engine SMOKE (P4-10).
"""

from __future__ import annotations

import unittest


class ViolationCapConstantsTests(unittest.TestCase):
    """Verify the cap constants are present in policy.config at the
    documented values. Constants live in config so an operator can
    tune them without code changes (e.g. for a known-large project)."""

    def setUp(self):
        # Direct import -- no synthetic-package dance needed; policy.config
        # is dependency-free dataclass-like module.
        from policy import config
        self.config = config

    def test_per_call_cap_is_100(self):
        # Per-call cap defends against single-resource explosions.
        # 100 is a generous-but-bounded ceiling: real resources rarely
        # produce more than 5-10 violations; 100 is pathological.
        self.assertEqual(self.config.MAX_VIOLATIONS_PER_CALL, 100)

    def test_per_run_cap_is_1000(self):
        # Per-run cap defends against many-resources scenarios.
        # 1000 is the punchlist's recommended default -- enough headroom
        # that no normal project hits it, low enough that a malicious
        # one is bounded.
        self.assertEqual(self.config.MAX_VIOLATIONS_PER_RUN, 1000)

    def test_per_run_cap_dominates_per_call_cap(self):
        # Sanity check: the per-run cap should comfortably exceed the
        # per-call cap so a single resource alone cannot exhaust the
        # run budget -- otherwise we'd silently lose information from
        # other resources just because the first one happened to be
        # noisy.
        self.assertGreater(
            self.config.MAX_VIOLATIONS_PER_RUN,
            self.config.MAX_VIOLATIONS_PER_CALL,
            "per-run cap must be > per-call cap so one noisy resource "
            "doesn't starve subsequent ones",
        )

    def test_caps_are_positive_ints(self):
        # Type-and-domain check: zero or negative caps would silently
        # drop ALL violations -- the opposite of what an operator
        # wants. Catch the misconfiguration here.
        for name in ("MAX_VIOLATIONS_PER_CALL", "MAX_VIOLATIONS_PER_RUN"):
            with self.subTest(constant=name):
                value = getattr(self.config, name)
                self.assertIsInstance(value, int)
                self.assertGreater(value, 0)


if __name__ == "__main__":
    unittest.main()
