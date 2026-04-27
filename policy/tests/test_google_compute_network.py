# policy/tests/test_google_compute_network.py
"""P4-5 structural tests for google_compute_network Rego rules."""

from __future__ import annotations

import os
import unittest


PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
RULE_DIR = os.path.join(PROJECT_ROOT, "policy", "policies", "google_compute_network")


def _read_rule(filename: str) -> str:
    path = os.path.join(RULE_DIR, filename)
    if not os.path.isfile(path):
        raise AssertionError(f"Rule file does not exist: {path}")
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


class NetworkNoDefaultVpcTests(unittest.TestCase):
    RULE_FILE = "network_no_default_vpc.rego"

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

    def test_cites_cis_3_1(self):
        self.assertIn("CIS GCP 3.1", self.contents)

    def test_checks_name_equals_default(self):
        # Sentinel: name == "default" identifies the auto-created VPC.
        self.assertIn('"default"', self.contents)


class NetworkRoutingModeRegionalTests(unittest.TestCase):
    RULE_FILE = "network_routing_mode_regional.rego"

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

    def test_checks_routing_config_routing_mode(self):
        # Mined field path: routingConfig.routingMode
        self.assertIn("routingConfig", self.contents)
        self.assertIn("routingMode", self.contents)

    def test_required_value_is_regional(self):
        # We default to REGIONAL (stricter than Google's archive
        # default of GLOBAL/parameterized).
        self.assertIn('"REGIONAL"', self.contents)


if __name__ == "__main__":
    unittest.main()
