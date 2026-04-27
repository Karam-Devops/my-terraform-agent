# policy/tests/test_provenance_enforcement.py
"""P4-8 (CG-3) walker test: enforce the three-source provenance
contract across EVERY .rego file in policy/policies/.

Why a single walker instead of per-rule structural tests:

  * Every per-tf_type test file (test_google_compute_firewall.py,
    test_google_container_cluster.py, ...) exercises the rule
    shape AT THE FILE LEVEL but only for the rules that file's
    author remembered to enumerate.
  * A new rule added next month, with the test author forgetting
    to add a corresponding structural test, would silently ship
    without provenance.
  * The walker closes that loop: every file in policy/policies/ is
    auto-discovered + auto-validated. Adding a rule WITHOUT
    provenance is a test failure even if no one wrote a per-rule
    test for it.

This is the CG-3 enforcement gate per the punchlist:
"new conftest test that asserts every .rego carries a complete
metadata block".

Excludes:
  * `_helpers.rego` files (helpers don't need provenance; they're
    pure functions, not policy rules)
  * Any file without `deny[` (allows for future support files in
    package main that aren't deny rules)
"""

from __future__ import annotations

import os
import unittest


PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
POLICIES_DIR = os.path.join(PROJECT_ROOT, "policy", "policies")


def _all_policy_rule_files() -> list[str]:
    """Walk policy/policies/ and return every .rego file path that
    represents a policy RULE (i.e. contains a deny[] block).

    Excludes:
      * Files starting with `_` (helper files like `_helpers.rego`)
      * Files that don't contain `deny[` (support modules that may
        ship later)
    """
    rule_files: list[str] = []
    for dirpath, _dirnames, filenames in os.walk(POLICIES_DIR):
        for fn in filenames:
            if not fn.endswith(".rego"):
                continue
            if fn.startswith("_"):
                continue
            full = os.path.join(dirpath, fn)
            with open(full, "r", encoding="utf-8") as f:
                if "deny[" not in f.read():
                    continue
            rule_files.append(full)
    return sorted(rule_files)


def _read(path: str) -> str:
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


# --- Sanity check on the walker itself --------------------------------

class WalkerDiscoveryTests(unittest.TestCase):
    """Defensive: ensure the walker actually finds rules. Without this
    the per-rule assertions would vacuously pass on an empty list.
    P4-7 shipped 44 total rules across the policies tree (P4-PRE 16 +
    P4-5 9 + P4-6 10 + P4-7 9). Pin a floor that catches regressions
    in either the walker logic OR an accidental rule deletion."""

    def setUp(self):
        self.rule_files = _all_policy_rule_files()

    def test_finds_at_least_44_rule_files(self):
        # Floor only -- new rules added by future commits should
        # bump this if it ever feels too loose.
        self.assertGreaterEqual(
            len(self.rule_files), 44,
            f"Walker only found {len(self.rule_files)} rule files; "
            f"expected >= 44 (P4-PRE through P4-7 baseline). Either "
            f"the walker logic broke or rules were accidentally "
            f"removed.",
        )

    def test_excludes_helper_files(self):
        # Belt-and-braces: no _helpers.rego should appear in the
        # walker's output.
        for path in self.rule_files:
            with self.subTest(path=path):
                self.assertFalse(
                    os.path.basename(path).startswith("_"),
                    f"Walker should exclude helper files; found {path}",
                )


# --- The per-rule contracts --------------------------------------------

class ProvenanceHeaderEnforcementTests(unittest.TestCase):
    """Every policy rule MUST carry the four provenance lines per the
    P4-PRE / P4-5 compact format:

        # Source: <archived template name> [derived?]   (or "NONE")
        # Standard: CIS <id> | NIST SP 800-53 <family>
        # Default: <chosen value> (rationale)
        # See docs/policy_provenance.md for full mining details.

    The test scans every rule file and asserts each label is present.
    `NIST` lives ON the Standard line in the compact format, so we
    don't check `NIST:` separately.
    """

    REQUIRED_LABELS = ("Source:", "Standard:", "Default:")

    def setUp(self):
        self.rule_files = _all_policy_rule_files()

    def test_every_rule_has_all_required_provenance_labels(self):
        for rule_path in self.rule_files:
            contents = _read(rule_path)
            for label in self.REQUIRED_LABELS:
                rel = os.path.relpath(rule_path, PROJECT_ROOT)
                with self.subTest(file=rel, label=label):
                    self.assertIn(
                        label, contents,
                        f"{rel} missing required provenance label "
                        f"`{label}`. Add a 4-line provenance block "
                        f"per the P4-5 convention -- see "
                        f"docs/phase4_handoff.md.",
                    )

    def test_every_rule_mentions_nist_family(self):
        # NIST family appears on the Standard line in the compact
        # format. Cross-cuts the Standard label to ensure both the
        # CIS / public benchmark AND the NIST control family are
        # cited per CG-3.
        for rule_path in self.rule_files:
            contents = _read(rule_path)
            rel = os.path.relpath(rule_path, PROJECT_ROOT)
            with self.subTest(file=rel):
                self.assertIn(
                    "NIST", contents,
                    f"{rel} does not cite a NIST SP 800-53 family. "
                    f"Add it to the Standard line: "
                    f"`# Standard: CIS <id> | NIST SP 800-53 <family>`",
                )


class DenyMessageContractTests(unittest.TestCase):
    """Every rule's deny[] message MUST follow the engine's expected
    `[SEVERITY][rule_id] text` prefix (see policy/engine.py:_VIOLATION_RE).
    Without it, the engine logs the message under the `unparsed/LOW`
    catch-all and the violation render in the UI loses its severity +
    rule context."""

    def setUp(self):
        self.rule_files = _all_policy_rule_files()

    def test_every_rule_message_carries_severity_prefix(self):
        # Acceptable severities per engine.py: HIGH | MED | LOW.
        # Verify SOMETHING in {[HIGH], [MED], [LOW]} appears in the
        # file (a deny block almost always emits one of these).
        valid_severities = ("[HIGH]", "[MED]", "[LOW]")
        for rule_path in self.rule_files:
            contents = _read(rule_path)
            rel = os.path.relpath(rule_path, PROJECT_ROOT)
            with self.subTest(file=rel):
                self.assertTrue(
                    any(sev in contents for sev in valid_severities),
                    f"{rel} has no severity prefix in any deny[] "
                    f"message. The engine's _VIOLATION_RE expects "
                    f"`[HIGH]` / `[MED]` / `[LOW]` -- otherwise the "
                    f"violation lands in the unparsed/LOW catch-all.",
                )


if __name__ == "__main__":
    unittest.main()
