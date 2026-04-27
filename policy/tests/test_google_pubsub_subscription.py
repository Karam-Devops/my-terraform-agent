# policy/tests/test_google_pubsub_subscription.py
"""P4-7 structural tests for google_pubsub_subscription Rego rules.
NOTE: Pub/Sub is NOT covered by GoogleCloudPlatform/policy-library
archived library. Source line says "NONE"."""

from __future__ import annotations

import os
import unittest


PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
RULE_DIR = os.path.join(PROJECT_ROOT, "policy", "policies", "google_pubsub_subscription")


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

    def test_source_acknowledges_no_archive(self):
        self.assertIn("NONE", self.contents)


class DeadLetterConfiguredTests(_BaseRuleTests, unittest.TestCase):
    RULE_FILE = "pubsub_sub_dead_letter_configured.rego"

    def test_checks_dead_letter_topic(self):
        self.assertIn("deadLetterPolicy", self.contents)
        self.assertIn("deadLetterTopic", self.contents)


class IamNoAllusersTests(_BaseRuleTests, unittest.TestCase):
    RULE_FILE = "pubsub_sub_iam_no_allusers.rego"

    def test_checks_public_principals(self):
        self.assertIn("allUsers", self.contents)
        self.assertIn("allAuthenticatedUsers", self.contents)


if __name__ == "__main__":
    unittest.main()
