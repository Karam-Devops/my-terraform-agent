# translator/tests/test_safe_invoke.py
"""P3-5 unit tests for the LLM retry wrapper + transient-error
classifier in llm_provider.py.

These cover the pure-logic parts of the P3-5 retry wrapper without
needing a real Vertex AI client. The wrapper's behaviour is too
important (it's the only thing keeping a 429 storm from cascading
into a workflow crash) to leave un-pinned.

Same import-isolation strategy as test_blueprint_diagnostic_path.py:
build a synthetic parent package + stub vertexai + langchain so
llm_provider.py imports without trying to talk to Google.
"""

from __future__ import annotations

import importlib.util
import os
import sys
import time
import types
import unittest
from unittest.mock import MagicMock, patch


PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))


# Synthetic parent package so llm_provider's `from .config import config`
# and `from .common.errors import ...` resolve.
_PARENT_PKG = "_p35_parent"
_LLM_PROVIDER_MOD = f"{_PARENT_PKG}.llm_provider"


def _load_llm_provider():
    """Load llm_provider.py without actually initialising Vertex AI.

    Stubs out vertexai + langchain_google_vertexai (heavyweight imports
    that would also try to talk to Google at module load) and provides
    a synthetic parent package for the relative imports.
    """
    cached = sys.modules.get(_LLM_PROVIDER_MOD)
    if cached is not None and hasattr(cached, "safe_invoke"):
        return cached

    # Stub heavy imports BEFORE loading llm_provider.
    if "vertexai" not in sys.modules:
        sys.modules["vertexai"] = MagicMock()
    if "langchain_google_vertexai" not in sys.modules:
        lgv = MagicMock()
        # The module's `from langchain_google_vertexai import ChatVertexAI`
        # needs ChatVertexAI to exist as an attribute.
        lgv.ChatVertexAI = MagicMock()
        sys.modules["langchain_google_vertexai"] = lgv

    # Synthetic parent package -- holds config + common as sub-packages.
    if _PARENT_PKG not in sys.modules:
        parent = types.ModuleType(_PARENT_PKG)
        parent.__path__ = [PROJECT_ROOT]
        sys.modules[_PARENT_PKG] = parent

    # Stub config module with the attributes safe_invoke reads.
    config_name = f"{_PARENT_PKG}.config"
    if config_name not in sys.modules:
        config_mod = types.ModuleType(config_name)
        config_inst = types.SimpleNamespace(
            GCP_PROJECT_ID="test-project",
            GCP_LOCATION="us-central1",
            GEMINI_MODEL="gemini-2.5-pro",
            LLM_MAX_RETRIES=3,
            LLM_TIMEOUT_SECONDS=120,
        )
        config_mod.config = config_inst
        sys.modules[config_name] = config_mod

    # Need real common.errors and common.logging -- they're testable
    # standalone (no relative imports beyond their own sibling modules).
    # Mount them under the synthetic parent package's namespace so
    # llm_provider's `from .common.errors import ...` resolves.
    common_pkg_name = f"{_PARENT_PKG}.common"
    if common_pkg_name not in sys.modules:
        common_pkg = types.ModuleType(common_pkg_name)
        common_pkg.__path__ = [os.path.join(PROJECT_ROOT, "common")]
        sys.modules[common_pkg_name] = common_pkg

    for sub in ("errors", "logging"):
        full = f"{common_pkg_name}.{sub}"
        if full not in sys.modules:
            spec = importlib.util.spec_from_file_location(
                full,
                os.path.join(PROJECT_ROOT, "common", f"{sub}.py"),
            )
            mod = importlib.util.module_from_spec(spec)
            sys.modules[full] = mod
            spec.loader.exec_module(mod)

    spec = importlib.util.spec_from_file_location(
        _LLM_PROVIDER_MOD,
        os.path.join(PROJECT_ROOT, "llm_provider.py"),
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules[_LLM_PROVIDER_MOD] = mod
    spec.loader.exec_module(mod)
    return mod


class IsTransientErrorTests(unittest.TestCase):
    """Pin the heuristic classifier. If a future maintainer narrows
    or widens this list, the tests fail with the offending token --
    forces an explicit decision rather than silent regression."""

    def setUp(self):
        self.provider = _load_llm_provider()

    def test_429_classified_transient(self):
        self.assertTrue(self.provider._is_transient_error(
            Exception("HTTP 429 Too Many Requests")
        ))

    def test_resource_exhausted_classified_transient(self):
        """Google API style (gRPC RESOURCE_EXHAUSTED)."""
        self.assertTrue(self.provider._is_transient_error(
            Exception("ResourceExhausted: quota exceeded")
        ))

    def test_deadline_exceeded_classified_transient(self):
        """gRPC DEADLINE_EXCEEDED (per-request timeout)."""
        self.assertTrue(self.provider._is_transient_error(
            Exception("DeadlineExceeded: deadline of 120s expired")
        ))

    def test_generic_timeout_classified_transient(self):
        for msg in ("connection timeout", "request timed out", "Read timed out"):
            with self.subTest(msg=msg):
                self.assertTrue(self.provider._is_transient_error(Exception(msg)))

    def test_5xx_codes_classified_transient(self):
        """Upstream / proxy / load-balancer hiccups."""
        for code in ("502", "503"):
            with self.subTest(code=code):
                self.assertTrue(self.provider._is_transient_error(
                    Exception(f"HTTP {code} returned")
                ))

    def test_grpc_unavailable_classified_transient(self):
        self.assertTrue(self.provider._is_transient_error(
            Exception("Unavailable: server is restarting")
        ))

    def test_400_not_transient(self):
        """Bad request is the caller's bug -- retrying won't help."""
        self.assertFalse(self.provider._is_transient_error(
            Exception("HTTP 400 Bad Request: invalid prompt")
        ))

    def test_401_not_transient(self):
        """Auth failure is also permanent under retry; needs creds fix."""
        self.assertFalse(self.provider._is_transient_error(
            Exception("HTTP 401 Unauthorized: invalid token")
        ))

    def test_404_not_transient(self):
        """Wrong model / wrong endpoint isn't fixable with backoff."""
        self.assertFalse(self.provider._is_transient_error(
            Exception("HTTP 404: model not found")
        ))

    def test_arbitrary_python_error_not_transient(self):
        self.assertFalse(self.provider._is_transient_error(
            ValueError("the prompt was None")
        ))


class SafeInvokeTests(unittest.TestCase):
    """Pin the retry-loop behaviour: when does it retry, when does it
    give up, what does it raise on giving up, what does it pass through
    on permanent errors."""

    def setUp(self):
        self.provider = _load_llm_provider()

    def test_success_on_first_attempt_returns_immediately(self):
        client = MagicMock()
        expected = MagicMock(content="hello")
        client.invoke.return_value = expected

        out = self.provider.safe_invoke(client, ["msg"])

        self.assertIs(out, expected)
        client.invoke.assert_called_once_with(["msg"])

    def test_permanent_error_propagates_immediately_no_retry(self):
        client = MagicMock()
        client.invoke.side_effect = ValueError("the prompt was None")

        with self.assertRaises(ValueError):
            self.provider.safe_invoke(client, ["msg"])

        # No retries -- single call, then raise.
        self.assertEqual(client.invoke.call_count, 1)

    def test_transient_then_success(self):
        """LLM 429s once, then succeeds on retry. Backoff schedule used."""
        client = MagicMock()
        ok = MagicMock(content="ok")
        client.invoke.side_effect = [
            Exception("429 Too Many Requests"),
            ok,
        ]

        with patch.object(self.provider.time, "sleep") as sleep_mock:
            out = self.provider.safe_invoke(
                client, ["msg"], max_attempts=3, base_delay_s=2.0,
            )

        self.assertIs(out, ok)
        self.assertEqual(client.invoke.call_count, 2)
        # First retry: base_delay_s * 2^0 = 2.0s
        sleep_mock.assert_called_once_with(2.0)

    def test_exhausted_retries_raise_upstream_timeout(self):
        client = MagicMock()
        # Always 429.
        client.invoke.side_effect = Exception("429 Too Many Requests")

        with patch.object(self.provider.time, "sleep"):
            with self.assertRaises(Exception) as ctx:
                self.provider.safe_invoke(
                    client, ["msg"], max_attempts=3, base_delay_s=0.0,
                )

        # Resolve UpstreamTimeout from the synthetic-parent common.errors.
        from importlib import import_module
        errors_mod = import_module(f"{_PARENT_PKG}.common.errors")
        self.assertIsInstance(ctx.exception, errors_mod.UpstreamTimeout)
        # Original transient exception preserved in __cause__
        self.assertIsNotNone(ctx.exception.__cause__)
        self.assertIn("429", str(ctx.exception.__cause__))
        # Should have called invoke max_attempts times
        self.assertEqual(client.invoke.call_count, 3)

    def test_exponential_backoff_schedule(self):
        """Retry delays double per attempt: 2s, 4s, 8s with base_delay_s=2.0."""
        client = MagicMock()
        # Fail 3 times then succeed -- so we observe 3 sleeps.
        ok = MagicMock(content="ok")
        client.invoke.side_effect = [
            Exception("503"),
            Exception("503"),
            Exception("503"),
            ok,
        ]

        with patch.object(self.provider.time, "sleep") as sleep_mock:
            out = self.provider.safe_invoke(
                client, ["msg"], max_attempts=4, base_delay_s=2.0,
            )

        self.assertIs(out, ok)
        # Three retries -> three sleep calls with doubled delays
        self.assertEqual(
            [call.args[0] for call in sleep_mock.call_args_list],
            [2.0, 4.0, 8.0],
        )

    def test_permanent_error_after_transient_propagates(self):
        """Got a 429 (transient), retried, then got a permanent 401 --
        the 401 should propagate as-is (no UpstreamTimeout wrapping)."""
        client = MagicMock()
        client.invoke.side_effect = [
            Exception("429 Too Many Requests"),
            Exception("HTTP 401 Unauthorized"),
        ]

        with patch.object(self.provider.time, "sleep"):
            with self.assertRaises(Exception) as ctx:
                self.provider.safe_invoke(
                    client, ["msg"], max_attempts=3, base_delay_s=0.0,
                )

        # Permanent error propagates with original message intact, NOT
        # wrapped in UpstreamTimeout.
        self.assertIn("401", str(ctx.exception))
        from importlib import import_module
        errors_mod = import_module(f"{_PARENT_PKG}.common.errors")
        self.assertNotIsInstance(ctx.exception, errors_mod.UpstreamTimeout)


if __name__ == "__main__":
    unittest.main()
