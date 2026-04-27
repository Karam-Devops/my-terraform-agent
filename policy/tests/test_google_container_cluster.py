# policy/tests/test_google_container_cluster.py
"""P4-6 structural tests for google_container_cluster Rego rules."""

from __future__ import annotations

import os
import unittest


PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
RULE_DIR = os.path.join(PROJECT_ROOT, "policy", "policies", "google_container_cluster")


def _read_rule(filename: str) -> str:
    path = os.path.join(RULE_DIR, filename)
    if not os.path.isfile(path):
        raise AssertionError(f"Rule file does not exist: {path}")
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


class _BaseRuleTests:
    """Mixin that pins the structural contract every rule must satisfy.
    Concrete subclasses override RULE_FILE."""
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


class WorkloadIdentityTests(_BaseRuleTests, unittest.TestCase):
    RULE_FILE = "cluster_workload_identity.rego"

    def test_checks_both_field_paths(self):
        # Mined: workloadPool (current) AND identityNamespace (legacy beta).
        self.assertIn("workloadPool", self.contents)
        self.assertIn("identityNamespace", self.contents)


class PrivateEndpointTests(_BaseRuleTests, unittest.TestCase):
    RULE_FILE = "cluster_private_endpoint.rego"

    def test_checks_private_nodes_field(self):
        self.assertIn("enablePrivateNodes", self.contents)

    def test_checks_private_endpoint_field(self):
        self.assertIn("enablePrivateEndpoint", self.contents)


class LegacyAbacDisabledTests(_BaseRuleTests, unittest.TestCase):
    RULE_FILE = "cluster_legacy_abac_disabled.rego"

    def test_checks_legacy_abac_field(self):
        self.assertIn("legacyAbac", self.contents)


class MasterAuthorizedNetworksTests(_BaseRuleTests, unittest.TestCase):
    RULE_FILE = "cluster_master_authorized_networks.rego"

    def test_checks_man_field(self):
        self.assertIn("masterAuthorizedNetworksConfig", self.contents)


if __name__ == "__main__":
    unittest.main()
