# policy/tests/test_google_kms_crypto_key.py
"""P4-6 structural tests for google_kms_crypto_key Rego rules."""

from __future__ import annotations

import os
import unittest


PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
RULE_DIR = os.path.join(PROJECT_ROOT, "policy", "policies", "google_kms_crypto_key")


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


class RotationMax90DaysTests(_BaseRuleTests, unittest.TestCase):
    RULE_FILE = "key_rotation_max_90_days.rego"

    def test_cites_cis_1_10(self):
        self.assertIn("CIS GCP 1.10", self.contents)

    def test_max_value_is_90_days_in_seconds(self):
        # 90 days * 86400 s/day = 7,776,000 s.
        # We deliberately chose 90d (CIS) vs Google's archive default
        # of 1 year. Verify the stricter value made it into the rule.
        self.assertIn("7776000", self.contents)

    def test_acknowledges_google_archive_default(self):
        # Provenance line + message both cite Google's looser default
        # for transparency. Operator reading the rule sees both
        # references without opening the docs.
        self.assertIn("31536000", self.contents)

    def test_handles_never_rotates_sentinel(self):
        # Mined sentinel: "99999999s" = "never rotates".
        self.assertIn("99999999", self.contents)


class ProtectionLevelHsmTests(_BaseRuleTests, unittest.TestCase):
    RULE_FILE = "key_protection_level_hsm.rego"

    def test_required_protection_is_hsm(self):
        self.assertIn('"HSM"', self.contents)

    def test_checks_version_template_field(self):
        # Mined field path: versionTemplate.protectionLevel
        self.assertIn("versionTemplate", self.contents)
        self.assertIn("protectionLevel", self.contents)


if __name__ == "__main__":
    unittest.main()
