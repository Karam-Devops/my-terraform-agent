# policy/tests/test_google_container_node_pool.py
"""P4-6 structural tests for google_container_node_pool Rego rules."""

from __future__ import annotations

import os
import unittest


PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
RULE_DIR = os.path.join(PROJECT_ROOT, "policy", "policies", "google_container_node_pool")


def _read_rule(filename: str) -> str:
    path = os.path.join(RULE_DIR, filename)
    if not os.path.isfile(path):
        raise AssertionError(f"Rule file does not exist: {path}")
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


class _BaseRuleTests:
    RULE_FILE: str = ""

    def setUp(self):
        self.contents = _read_rule(self.RULE_FILE)

    def test_declares_package_main(self):
        self.assertIn("package main", self.contents)

    def test_has_deny_rule(self):
        self.assertIn("deny[msg]", self.contents)

    def test_carries_provenance(self):
        for label in ("Source:", "Standard:", "Default:"):
            with self.subTest(label=label):
                self.assertIn(label, self.contents)


class AutoUpgradeTests(_BaseRuleTests, unittest.TestCase):
    RULE_FILE = "node_pool_auto_upgrade.rego"

    def test_checks_auto_upgrade_field(self):
        self.assertIn("autoUpgrade", self.contents)


class AutoRepairTests(_BaseRuleTests, unittest.TestCase):
    RULE_FILE = "node_pool_auto_repair.rego"

    def test_checks_auto_repair_field(self):
        self.assertIn("autoRepair", self.contents)


class UsesCosTests(_BaseRuleTests, unittest.TestCase):
    RULE_FILE = "node_pool_uses_cos.rego"

    def test_allowed_image_types_set(self):
        # Mined: COS + COS_CONTAINERD are the two acceptable values.
        self.assertIn('"COS"', self.contents)
        self.assertIn('"COS_CONTAINERD"', self.contents)

    def test_checks_image_type_field(self):
        self.assertIn("imageType", self.contents)


class NoDefaultSaTests(_BaseRuleTests, unittest.TestCase):
    RULE_FILE = "node_pool_no_default_sa.rego"

    def test_checks_service_account_field(self):
        self.assertIn("serviceAccount", self.contents)

    def test_default_sa_sentinel(self):
        # The "default" SA name is the canonical violation.
        self.assertIn('"default"', self.contents)


if __name__ == "__main__":
    unittest.main()
