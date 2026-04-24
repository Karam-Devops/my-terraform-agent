# common/tests/test_logging.py
"""Unit tests for common.logging.

Three behaviours get dedicated tests. The rest (level filtering,
timestamp shape, renderer selection) is exercised indirectly by the
engines' own integration tests.

    1. JSON mode produces one parseable JSON object per log call.
       If this breaks, Cloud Logging stops parsing our payloads and
       every filter-by-tenant query silently returns empty.

    2. Context binding merges into every subsequent log call in the
       same task. This is the SaaS multi-tenant gate -- if context
       leaks or doesn't attach, operators can't answer "show me all
       failed requests for tenant X".

    3. clear_context fully clears. This is the stale-context-bleed
       failure mode on Cloud Run: container serves tenant A, then
       tenant B -- B must not see A's context.

We intentionally do NOT test:
    - Exact ANSI colour codes in console mode (environment-dependent).
    - Third-party (stdlib) logger routing (structlog's own test suite
      covers it, not ours).
"""

from __future__ import annotations

import io
import json
import logging
import os
import unittest
from unittest.mock import patch

import structlog


def _reload_logging_module(env_overrides: dict) -> object:
    """Reimport common.logging under a specific env so _configure() re-runs.

    common.logging configures structlog at import time, so to exercise
    JSON vs console we have to reload the module with the env we want.
    Returns the reloaded module.
    """
    import importlib
    import sys

    # Prime env
    old = {k: os.environ.get(k) for k in env_overrides}
    for k, v in env_overrides.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v

    try:
        if "common.logging" in sys.modules:
            mod = importlib.reload(sys.modules["common.logging"])
        else:
            import common.logging as mod  # type: ignore
        return mod
    finally:
        # Restore env for downstream tests
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


class LoggingJSONModeTests(unittest.TestCase):
    """Pin the Cloud Run production path: JSON-lines on stdout."""

    def setUp(self):
        # Force JSON mode; capture stdlib's root logger output into a
        # stream we can assert on. structlog routes through stdlib, so
        # patching basicConfig's stream is the clean seam.
        self.buf = io.StringIO()
        self._logmod = _reload_logging_module({"MTAGENT_LOG_FORMAT": "json"})
        # Replace root handler stream so we can capture output.
        root = logging.getLogger()
        for h in list(root.handlers):
            root.removeHandler(h)
        handler = logging.StreamHandler(self.buf)
        handler.setFormatter(logging.Formatter("%(message)s"))
        root.addHandler(handler)
        root.setLevel(logging.INFO)
        # Each test starts with a clean context
        self._logmod.clear_context()

    def tearDown(self):
        self._logmod.clear_context()

    def test_info_call_produces_parseable_json(self):
        """A single log.info call emits exactly one JSON object with the event."""
        log = self._logmod.get_logger("smoke")
        log.info("import_started", resource_type="google_compute_instance")

        lines = [ln for ln in self.buf.getvalue().splitlines() if ln.strip()]
        self.assertEqual(len(lines), 1, "one log call should emit one line")
        payload = json.loads(lines[0])
        self.assertEqual(payload["event"], "import_started")
        self.assertEqual(payload["resource_type"], "google_compute_instance")
        self.assertEqual(payload["level"], "info")
        self.assertIn("timestamp", payload,
                      "Cloud Logging parses timestamp; must be present")

    def test_bind_context_merges_into_subsequent_calls(self):
        """bind_context fields appear on every log line until cleared."""
        log = self._logmod.get_logger("smoke")

        self._logmod.bind_context(
            engine="importer",
            tenant_id="acme-corp",
            project_id="dev-proj-470211",
            request_id="req-abc123",
        )
        log.info("resource_imported", name="web-1")
        log.warning("resource_skipped", name="api-2", reason="403 Forbidden")

        lines = [ln for ln in self.buf.getvalue().splitlines() if ln.strip()]
        self.assertEqual(len(lines), 2)
        for ln in lines:
            payload = json.loads(ln)
            self.assertEqual(payload["engine"], "importer")
            self.assertEqual(payload["tenant_id"], "acme-corp")
            self.assertEqual(payload["project_id"], "dev-proj-470211")
            self.assertEqual(payload["request_id"], "req-abc123")


class LoggingContextIsolationTests(unittest.TestCase):
    """Pin the "no tenant bleed" contract for Cloud Run request-reuse."""

    def setUp(self):
        self.buf = io.StringIO()
        self._logmod = _reload_logging_module({"MTAGENT_LOG_FORMAT": "json"})
        root = logging.getLogger()
        for h in list(root.handlers):
            root.removeHandler(h)
        root.addHandler(logging.StreamHandler(self.buf))
        root.setLevel(logging.INFO)
        self._logmod.clear_context()

    def tearDown(self):
        self._logmod.clear_context()

    def test_clear_context_removes_all_bound_keys(self):
        """After clear_context, subsequent logs have no tenant/project fields.

        The failure this pins: Cloud Run container serves tenant A's
        request, then tenant B's. If A's bind_context survives past
        the request boundary, B's logs appear under A's tenant in the
        operator's dashboard -- a data-isolation violation.
        """
        log = self._logmod.get_logger("smoke")

        self._logmod.bind_context(tenant_id="acme-corp", project_id="proj-a")
        log.info("tenant_a_event")
        self._logmod.clear_context()
        log.info("unbound_event")

        lines = [ln for ln in self.buf.getvalue().splitlines() if ln.strip()]
        self.assertEqual(len(lines), 2)
        first = json.loads(lines[0])
        second = json.loads(lines[1])

        self.assertEqual(first["tenant_id"], "acme-corp")
        self.assertEqual(first["project_id"], "proj-a")
        self.assertNotIn("tenant_id", second,
                         "clear_context() must remove tenant_id")
        self.assertNotIn("project_id", second,
                         "clear_context() must remove project_id")

    def test_unbind_context_removes_only_specified_keys(self):
        """unbind_context keeps the keys we didn't pass."""
        log = self._logmod.get_logger("smoke")

        self._logmod.bind_context(
            tenant_id="acme",
            project_id="proj-a",
            stage="scan",
        )
        self._logmod.unbind_context("stage")
        log.info("after_unbind")

        lines = [ln for ln in self.buf.getvalue().splitlines() if ln.strip()]
        payload = json.loads(lines[-1])
        self.assertEqual(payload["tenant_id"], "acme")
        self.assertEqual(payload["project_id"], "proj-a")
        self.assertNotIn("stage", payload)


if __name__ == "__main__":
    unittest.main()
