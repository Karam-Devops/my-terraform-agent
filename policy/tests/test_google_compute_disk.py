# policy/tests/test_google_compute_disk.py
"""P4-5 structural tests for google_compute_disk Rego rules.

Same structural-only pattern as test_google_compute_firewall.py.
Semantic tests run in P4-10 SMOKE against real cloud snapshots.
P4-8 will generalize these checks via a single walker test that
scans every .rego in policy/policies/.
"""

from __future__ import annotations

import os
import unittest


PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
RULE_DIR = os.path.join(PROJECT_ROOT, "policy", "policies", "google_compute_disk")


def _read_rule(filename: str) -> str:
    path = os.path.join(RULE_DIR, filename)
    if not os.path.isfile(path):
        raise AssertionError(f"Rule file does not exist: {path}")
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


class DiskCmekRequiredTests(unittest.TestCase):
    RULE_FILE = "disk_cmek_required.rego"

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

    def test_cites_cis_4_7(self):
        self.assertIn("CIS GCP 4.7", self.contents)

    def test_checks_disk_encryption_key_field(self):
        # Mined field path: diskEncryptionKey.kmsKeyName
        self.assertIn("diskEncryptionKey", self.contents)
        self.assertIn("kmsKeyName", self.contents)

    def test_severity_is_high(self):
        # CMEK gap = audit-failing on regulated workloads.
        self.assertIn("[HIGH]", self.contents)


class DiskSnapshotPolicyAttachedTests(unittest.TestCase):
    RULE_FILE = "disk_snapshot_policy_attached.rego"

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

    def test_checks_resource_policies_field(self):
        # Mined field path: resourcePolicies (list)
        self.assertIn("resourcePolicies", self.contents)

    def test_checks_for_empty_list(self):
        # The rule fires when resourcePolicies is empty (no scheduled
        # snapshot policy attached). Verify the count == 0 idiom.
        self.assertIn("count(", self.contents)


if __name__ == "__main__":
    unittest.main()
