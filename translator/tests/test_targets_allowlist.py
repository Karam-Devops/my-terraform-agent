# translator/tests/test_targets_allowlist.py
"""P4-15.2 / CG-8H prep: tests for the TRANSLATOR_TARGETS_ALLOWED
config gate that lets the SaaS UI hide Azure for Round-1 without
removing engine support.

Direct tests against translator.config -- no synthetic-package
dance needed because the module is dependency-free except for
``common.terraform_path`` (which is also dependency-free).
"""

from __future__ import annotations

import importlib
import os
import sys
import unittest
from unittest.mock import patch


class TranslatorTargetsAllowlistTests(unittest.TestCase):
    """The allowlist controls which target clouds the CLI/UI offers
    as choices. Backend (run_translation_batch) supports both
    unconditionally; this list is purely surface-level UX gating."""

    def _reload_config(self):
        """Reload translator.config so it re-reads the env var."""
        import translator.config as cfg
        return importlib.reload(cfg)

    def test_default_includes_both_targets(self):
        # No env var -> CLI shows both AWS + Azure (operator default).
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("TRANSLATOR_TARGETS_ALLOWED", None)
            cfg = self._reload_config()
            self.assertEqual(
                sorted(cfg.TRANSLATOR_TARGETS_ALLOWED),
                ["aws", "azure"],
            )

    def test_aws_only_for_saas_round1(self):
        # SaaS Round-1 sets the var to hide Azure from customers.
        with patch.dict(os.environ,
                        {"TRANSLATOR_TARGETS_ALLOWED": "aws"}):
            cfg = self._reload_config()
            self.assertEqual(cfg.TRANSLATOR_TARGETS_ALLOWED, ["aws"])
            self.assertNotIn("azure", cfg.TRANSLATOR_TARGETS_ALLOWED)

    def test_azure_only_explicit(self):
        # Just verifying the allowlist mechanism is symmetric --
        # a future deployment could choose Azure-only.
        with patch.dict(os.environ,
                        {"TRANSLATOR_TARGETS_ALLOWED": "azure"}):
            cfg = self._reload_config()
            self.assertEqual(cfg.TRANSLATOR_TARGETS_ALLOWED, ["azure"])

    def test_normalises_case_and_whitespace(self):
        # Defensive: env vars set via UI / dashboard often carry
        # accidental whitespace + uppercase. The list comp lowers +
        # strips so the comparison in the CLI is robust.
        with patch.dict(os.environ,
                        {"TRANSLATOR_TARGETS_ALLOWED": "  AWS , Azure  "}):
            cfg = self._reload_config()
            self.assertEqual(cfg.TRANSLATOR_TARGETS_ALLOWED,
                             ["aws", "azure"])

    def test_empty_string_yields_empty_list(self):
        # Edge case: explicit empty value. CLI guards against this
        # by falling back to ['aws', 'azure'] -- the config layer
        # just reflects what was set.
        with patch.dict(os.environ,
                        {"TRANSLATOR_TARGETS_ALLOWED": ""}):
            cfg = self._reload_config()
            self.assertEqual(cfg.TRANSLATOR_TARGETS_ALLOWED, [])

    def tearDown(self):
        # Restore default config for any subsequent tests in this
        # module / process.
        os.environ.pop("TRANSLATOR_TARGETS_ALLOWED", None)
        if "translator.config" in sys.modules:
            importlib.reload(sys.modules["translator.config"])


if __name__ == "__main__":
    unittest.main()
