# policy/tests/test_google_cloud_run_v2_service.py
"""P4-7 structural tests for google_cloud_run_v2_service Rego rules.

NOTE: Cloud Run is NOT covered by GoogleCloudPlatform/policy-library
(archived). Source line in each rule says "NONE"; Standard cites
CIS Controls v8 + Google Best Practices URL.
"""

from __future__ import annotations

import os
import unittest


PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
RULE_DIR = os.path.join(PROJECT_ROOT, "policy", "policies", "google_cloud_run_v2_service")


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
        # Cloud Run isn't in the GCP archive, so the Source line
        # MUST say so explicitly (not silently fake an archive
        # citation). Three-source provenance contract: be honest
        # about which sources actually back the rule.
        self.assertIn("NONE", self.contents)


class NoPublicInvokerTests(_BaseRuleTests, unittest.TestCase):
    RULE_FILE = "cloudrun_no_public_invoker.rego"

    def test_checks_run_invoker_role(self):
        self.assertIn("roles/run.invoker", self.contents)

    def test_checks_public_principals(self):
        # Same canonical sentinels mined from
        # storage_world_readable_v1 in P4-PRE.
        self.assertIn("allUsers", self.contents)
        self.assertIn("allAuthenticatedUsers", self.contents)


class MinInstancesDocumentedTests(_BaseRuleTests, unittest.TestCase):
    RULE_FILE = "cloudrun_min_instances_documented.rego"

    def test_checks_min_instance_count(self):
        # Mined field path (current Best Practices docs):
        # template.scaling.minInstanceCount
        self.assertIn("minInstanceCount", self.contents)

    def test_severity_is_low(self):
        # Operational hygiene -- LOW. Not a security gap.
        self.assertIn("[LOW]", self.contents)


if __name__ == "__main__":
    unittest.main()
