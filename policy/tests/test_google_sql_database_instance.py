# policy/tests/test_google_sql_database_instance.py
"""P4-7 structural tests for google_sql_database_instance Rego rules."""

from __future__ import annotations

import os
import unittest


PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
RULE_DIR = os.path.join(PROJECT_ROOT, "policy", "policies", "google_sql_database_instance")


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


class NoPublicIpTests(_BaseRuleTests, unittest.TestCase):
    RULE_FILE = "sql_no_public_ip.rego"

    def test_cites_cis_6_5(self):
        self.assertIn("CIS GCP 6.5", self.contents)

    def test_checks_ipv4_enabled(self):
        # Mined: settings.ipConfiguration.ipv4Enabled
        self.assertIn("ipv4Enabled", self.contents)


class SslRequiredTests(_BaseRuleTests, unittest.TestCase):
    RULE_FILE = "sql_ssl_required.rego"

    def test_cites_cis_6_4(self):
        self.assertIn("CIS GCP 6.4", self.contents)

    def test_checks_require_ssl(self):
        self.assertIn("requireSsl", self.contents)


class BackupEnabledTests(_BaseRuleTests, unittest.TestCase):
    RULE_FILE = "sql_backup_enabled.rego"

    def test_cites_cis_6_7(self):
        self.assertIn("CIS GCP 6.7", self.contents)

    def test_checks_backup_configuration(self):
        self.assertIn("backupConfiguration", self.contents)


if __name__ == "__main__":
    unittest.main()
