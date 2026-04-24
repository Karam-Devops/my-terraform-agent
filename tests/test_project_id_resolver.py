# tests/test_project_id_resolver.py
"""
Smoke tests for TODO #11 — HOST/TARGET/DEMO project-ID separation and
the `resolve_target_project_id()` safety gate.

We reload `config` under each test with a controlled env so the
class-level defaults are recomputed deterministically. This is uglier
than dependency injection but keeps the production code path (env-var
driven module-level defaults) honest — the resolver reads from
`config.config.*` at call time, so the test must mutate the class
attributes the way a real env change would.
"""

from __future__ import annotations

import importlib
import os
import sys
import unittest
from unittest.mock import patch


PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)


def _reload_config_with_env(env: dict):
    """Reload `config` with a controlled environment.

    Patches os.environ to exactly `env` for the import duration, then
    re-imports config so the module-level Config class re-reads
    os.getenv. Returns the freshly loaded module.
    """
    # Drop the cached module so importlib actually re-executes it under
    # the patched environment. Without this the class attributes stay
    # frozen at first-import values and the test reads stale data.
    sys.modules.pop("config", None)
    with patch.dict(os.environ, env, clear=True):
        import config as cfg
        importlib.reload(cfg)
        return cfg


class ResolverHappyPaths(unittest.TestCase):
    def test_user_supplied_takes_precedence_over_env(self):
        cfg = _reload_config_with_env({"TARGET_PROJECT_ID": "from-env"})
        self.assertEqual(cfg.resolve_target_project_id("from-user"), "from-user")

    def test_empty_input_falls_back_to_target_env(self):
        cfg = _reload_config_with_env({"TARGET_PROJECT_ID": "from-env"})
        self.assertEqual(cfg.resolve_target_project_id(""), "from-env")
        self.assertEqual(cfg.resolve_target_project_id(None), "from-env")
        # Whitespace-only is treated as empty.
        self.assertEqual(cfg.resolve_target_project_id("   "), "from-env")

    def test_whitespace_around_user_input_is_stripped(self):
        cfg = _reload_config_with_env({})
        self.assertEqual(cfg.resolve_target_project_id("  foo  "), "foo")

    def test_legacy_gcp_project_id_env_populates_target(self):
        # Back-compat: the legacy GCP_PROJECT_ID env should still drive
        # the importer if neither TARGET_PROJECT_ID nor user input is set.
        cfg = _reload_config_with_env({"GCP_PROJECT_ID": "legacy-env"})
        self.assertEqual(cfg.config.TARGET_PROJECT_ID, "legacy-env")
        self.assertEqual(cfg.resolve_target_project_id(""), "legacy-env")

    def test_target_env_overrides_legacy_when_both_set(self):
        # If both env vars exist, TARGET_PROJECT_ID wins (intentional —
        # it's the explicit, modern name).
        cfg = _reload_config_with_env({
            "GCP_PROJECT_ID": "legacy",
            "TARGET_PROJECT_ID": "modern",
        })
        self.assertEqual(cfg.config.TARGET_PROJECT_ID, "modern")
        self.assertEqual(cfg.resolve_target_project_id(""), "modern")


class ResolverErrorPaths(unittest.TestCase):
    def test_no_input_no_env_raises(self):
        cfg = _reload_config_with_env({})
        with self.assertRaises(ValueError) as ctx:
            cfg.resolve_target_project_id("")
        msg = str(ctx.exception)
        self.assertIn("No GCP project ID", msg)
        # Error message must point the user at the fix.
        self.assertIn("TARGET_PROJECT_ID", msg)


class DemoSafetyLock(unittest.TestCase):
    def test_demo_lock_match_returns_value(self):
        cfg = _reload_config_with_env({
            "DEMO_PROJECT_ID": "demo-acme",
            "TARGET_PROJECT_ID": "demo-acme",
        })
        # Both env-fallback and explicit-input paths must succeed when
        # the value matches the lock.
        self.assertEqual(cfg.resolve_target_project_id(""), "demo-acme")
        self.assertEqual(cfg.resolve_target_project_id("demo-acme"), "demo-acme")

    def test_demo_lock_mismatch_user_input_raises(self):
        cfg = _reload_config_with_env({"DEMO_PROJECT_ID": "demo-acme"})
        with self.assertRaises(ValueError) as ctx:
            cfg.resolve_target_project_id("prod-customer")
        msg = str(ctx.exception)
        self.assertIn("DEMO_PROJECT_ID", msg)
        self.assertIn("prod-customer", msg)
        self.assertIn("demo-acme", msg)

    def test_demo_lock_mismatch_env_fallback_raises(self):
        # Even if TARGET_PROJECT_ID env is set to a "valid" project, the
        # demo lock must override and refuse it.
        cfg = _reload_config_with_env({
            "DEMO_PROJECT_ID": "demo-acme",
            "TARGET_PROJECT_ID": "prod-customer",
        })
        with self.assertRaises(ValueError):
            cfg.resolve_target_project_id("")

    def test_demo_lock_unset_means_anything_goes(self):
        # Sanity: with no DEMO_PROJECT_ID, any non-empty input is accepted.
        cfg = _reload_config_with_env({})
        self.assertEqual(cfg.resolve_target_project_id("anything-at-all"), "anything-at-all")


class ConceptSeparation(unittest.TestCase):
    def test_host_and_target_are_independent(self):
        cfg = _reload_config_with_env({
            "HOST_PROJECT_ID": "saas-tenant",
            "TARGET_PROJECT_ID": "client-tenant",
        })
        self.assertEqual(cfg.config.HOST_PROJECT_ID, "saas-tenant")
        self.assertEqual(cfg.config.TARGET_PROJECT_ID, "client-tenant")
        # Back-compat alias still points at HOST (its historical role:
        # Vertex AI init in llm_provider.py).
        self.assertEqual(cfg.config.GCP_PROJECT_ID, "saas-tenant")

    def test_legacy_gcp_project_id_seeds_both_when_alone(self):
        # Pre-migration deployments only set GCP_PROJECT_ID. It must
        # populate HOST (for Vertex AI) AND TARGET (for the importer
        # default). Otherwise the migration breaks dev environments
        # that didn't update their env yet.
        cfg = _reload_config_with_env({"GCP_PROJECT_ID": "legacy-only"})
        self.assertEqual(cfg.config.HOST_PROJECT_ID, "legacy-only")
        self.assertEqual(cfg.config.TARGET_PROJECT_ID, "legacy-only")
        self.assertEqual(cfg.config.GCP_PROJECT_ID, "legacy-only")


if __name__ == "__main__":
    unittest.main()
