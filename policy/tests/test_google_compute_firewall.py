# policy/tests/test_google_compute_firewall.py
"""P4-5 (preview) structural tests for the google_compute_firewall rules.

No local conftest/opa available, so these tests verify FILE STRUCTURE
+ PROVENANCE rather than rule semantics. Semantic tests (does this
rule fire on a sample input?) run in P4-10 SMOKE against real
terraform plan + conftest invocation.

Per-rule structural test pattern that scales to all P4-5/6/7 rules:
  * The .rego file exists at the canonical path
  * Has the three-source provenance block (Source / Standard /
    NIST / Default)
  * Declares `package main` so the engine picks it up
  * Has at least one `deny[msg]` rule

P4-8 will GENERALIZE this pattern into a single test that walks
every .rego in policy/policies/ and applies the same checks. For
the P4-5 preview, this targeted file proves the pattern works.
"""

from __future__ import annotations

import os
import unittest


PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
RULE_DIR = os.path.join(PROJECT_ROOT, "policy", "policies", "google_compute_firewall")


def _read_rule(filename: str) -> str:
    """Read a rule file's contents as a string. Asserts the file exists."""
    path = os.path.join(RULE_DIR, filename)
    if not os.path.isfile(path):
        raise AssertionError(f"Rule file does not exist: {path}")
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


class FirewallNoOpenSshStructureTests(unittest.TestCase):
    """P4-5 representative-rule preview: prove the structural test
    pattern that the remaining ~24 P4-5/6/7 rules will follow."""

    RULE_FILE = "firewall_no_open_ssh.rego"

    def setUp(self):
        self.contents = _read_rule(self.RULE_FILE)

    def test_declares_package_main(self):
        # All policy rules MUST be in `package main` so the engine's
        # output parser (engine.py:_VIOLATION_RE) picks up the deny
        # messages. New-package files would be silently ignored.
        self.assertIn("package main", self.contents,
                      "Rule must declare `package main`")

    def test_has_at_least_one_deny_rule(self):
        # Without a deny rule, the file is policy-shaped but does
        # nothing. Catch authoring mistakes early.
        self.assertIn("deny[msg]", self.contents,
                      "Rule must declare at least one deny[msg] block")

    def test_carries_three_source_provenance_block(self):
        # P4-5 compact format: 3 lines (Source / Standard line that
        # also carries NIST / Default). The CG-3 contract: every rule
        # carries Source + Standard + Default citations so violations
        # render with control IDs in the UI. P4-8 will enforce this
        # walker-style across ALL .rego files (excluding _helpers.rego).
        for label in ("Source:", "Standard:", "Default:"):
            with self.subTest(label=label):
                self.assertIn(label, self.contents,
                              f"Rule must carry `{label}` provenance line")

    def test_provenance_cites_specific_cis_control(self):
        # CIS GCP 3.6 is the canonical control for "SSH not open to
        # internet". Pinning the exact number in the rule keeps the
        # demo's compliance audit story tight.
        self.assertIn("CIS GCP 3.6", self.contents)

    def test_provenance_cites_nist_control(self):
        # NIST family appears on the Standard line in the compact
        # format (Standard: CIS GCP 3.6 | NIST SP 800-53 SC-7).
        self.assertIn("NIST", self.contents)

    def test_message_includes_severity_and_rule_id(self):
        # engine.py:_VIOLATION_RE expects `[SEVERITY][rule_id] text`.
        # Verify the rule's deny message follows the prefix
        # convention (else the engine logs it as `unparsed/LOW`).
        self.assertIn("[HIGH]", self.contents)
        self.assertIn("[firewall_no_open_ssh]", self.contents)

    def test_message_carries_control_id_suffix(self):
        # Standardized P4-5 deny format: `... (CIS <id>)` at the end
        # so the operator sees the audit reference without needing to
        # open the rule file.
        self.assertIn("(CIS GCP 3.6)", self.contents)


class FirewallNoOpenRdpStructureTests(unittest.TestCase):
    """Sibling rule to no_open_ssh -- same shape, different port (3389)."""

    RULE_FILE = "firewall_no_open_rdp.rego"

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

    def test_targets_port_3389(self):
        # Sanity: must hit RDP, not SSH.
        self.assertIn('"3389"', self.contents)

    def test_cites_cis_3_7(self):
        self.assertIn("CIS GCP 3.7", self.contents)

    def test_uses_shared_helper(self):
        # Sibling rules MUST use the shared allows_tcp_port_to_world
        # helper rather than re-implementing the port-match logic.
        # Otherwise we'd have two implementations to maintain.
        self.assertIn("allows_tcp_port_to_world(", self.contents)


class FirewallLogsEnabledStructureTests(unittest.TestCase):
    RULE_FILE = "firewall_logs_enabled.rego"

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

    def test_checks_log_config_enable_field(self):
        # Mined verbatim from gcp_network_enable_firewall_logs_v1.
        self.assertIn("logConfig", self.contents)
        self.assertIn('"enable"', self.contents)


class FirewallHelpersTests(unittest.TestCase):
    """Shared helpers in _helpers.rego -- extracted P4-5 once the second
    sibling rule (no_open_rdp) made duplication concrete. Verifies the
    defensive-defaulting pattern lives in the right place."""

    def setUp(self):
        self.contents = _read_rule("_helpers.rego")

    def test_helpers_declare_package_main(self):
        # Helpers MUST be in package main so per-rule deny[] blocks can
        # reference them without imports.
        self.assertIn("package main", self.contents)

    def test_helpers_use_defensive_defaulting_pattern(self):
        # P4-PRE established `object.get(parent, key, default)` as the
        # canonical defensive-defaulting pattern. Verify the helpers
        # file uses it consistently.
        self.assertIn("object.get(", self.contents)

    def test_helpers_define_shared_predicates(self):
        # The contract sibling rules (firewall_no_open_ssh,
        # firewall_no_open_rdp, future port-specific rules) depend on:
        for predicate in ("default_list", "firewall_enabled",
                          "sources_open_to_internet", "permits_tcp",
                          "permits_port", "allows_tcp_port_to_world"):
            with self.subTest(predicate=predicate):
                self.assertIn(predicate, self.contents)


if __name__ == "__main__":
    unittest.main()
