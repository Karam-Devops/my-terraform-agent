# policy/tests/test_google_compute_subnetwork.py
"""P4-5 structural tests for google_compute_subnetwork Rego rules."""

from __future__ import annotations

import os
import unittest


PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
RULE_DIR = os.path.join(PROJECT_ROOT, "policy", "policies", "google_compute_subnetwork")


def _read_rule(filename: str) -> str:
    path = os.path.join(RULE_DIR, filename)
    if not os.path.isfile(path):
        raise AssertionError(f"Rule file does not exist: {path}")
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


class SubnetFlowLogsEnabledTests(unittest.TestCase):
    RULE_FILE = "subnet_flow_logs_enabled.rego"

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

    def test_cites_cis_3_8(self):
        self.assertIn("CIS GCP 3.8", self.contents)

    def test_checks_modern_log_config(self):
        # Mined field path: logConfig.enable (modern API)
        self.assertIn("logConfig", self.contents)
        self.assertIn('"enable"', self.contents)

    def test_checks_legacy_enable_flow_logs(self):
        # Mined field path: enableFlowLogs (legacy API)
        # Google's template checks both; we mirror that.
        self.assertIn("enableFlowLogs", self.contents)

    def test_exempts_managed_proxy_subnets(self):
        # Mined exemption from Google's template: managed-proxy
        # subnetworks are control-plane only, no traffic to log.
        self.assertIn("REGIONAL_MANAGED_PROXY", self.contents)
        self.assertIn("INTERNAL_HTTPS_LOAD_BALANCER", self.contents)


class SubnetPrivateGoogleAccessTests(unittest.TestCase):
    RULE_FILE = "subnet_private_google_access.rego"

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

    def test_checks_private_ip_google_access_field(self):
        # Mined field path: privateIpGoogleAccess
        self.assertIn("privateIpGoogleAccess", self.contents)


if __name__ == "__main__":
    unittest.main()
