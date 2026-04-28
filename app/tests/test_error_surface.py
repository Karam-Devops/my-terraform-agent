# app/tests/test_error_surface.py
"""Unit tests for app.ui.error_surface (PUI-1).

The renderer's job: branch on exception type and call the right
Streamlit display widgets (st.error / st.info / st.expander / st.code).
We mock those widgets and assert what got called with what.

Why mock instead of using AppTest: AppTest spins up a Streamlit
runtime which is heavy and overkill for what is essentially a
"calls the right function with the right strings" check. The mocks
verify the contract; the actual rendering is visually inspected
during the page-level smoke.
"""

from __future__ import annotations

import unittest
from unittest.mock import patch, MagicMock

from common.errors import PreflightError
from app.ui import error_surface


class RenderErrorPreflightTests(unittest.TestCase):
    """PreflightError -> curated path: st.error + st.info(hint)."""

    def test_preflighterror_renders_message_and_hint(self):
        exc = PreflightError(
            "project ID validation failed: bad input",
            stage="validate_project_id",
            reason="bad input",
        )
        # PreflightError uses class-level user_hint; override per-instance
        # to assert the hint surfaces in st.info.
        exc.user_hint = "Try a valid GCP project ID like 'my-proj-123456'."

        with patch.object(error_surface.st, "error") as m_err, \
             patch.object(error_surface.st, "info") as m_info, \
             patch.object(error_surface.st, "caption") as m_cap:
            error_surface.render_error(exc, context="running the importer")

        # Error banner should include the context prefix + the message.
        m_err.assert_called_once()
        err_arg = m_err.call_args[0][0]
        self.assertIn("running the importer", err_arg)
        self.assertIn("project ID validation failed", err_arg)

        # Hint should surface via st.info with the "Next step:" framing.
        m_info.assert_called_once()
        info_arg = m_info.call_args[0][0]
        self.assertIn("Next step", info_arg)
        self.assertIn("Try a valid GCP project ID", info_arg)

        # Stage caption should fire for diagnostics.
        m_cap.assert_called_once()
        cap_arg = m_cap.call_args[0][0]
        self.assertIn("validate_project_id", cap_arg)

    def test_preflighterror_without_context_still_renders(self):
        """No context arg -> message stands alone (no 'While ' prefix)."""
        exc = PreflightError("workdir resolution failed", stage="resolve_workdir")
        with patch.object(error_surface.st, "error") as m_err, \
             patch.object(error_surface.st, "info"), \
             patch.object(error_surface.st, "caption"):
            error_surface.render_error(exc)
        err_arg = m_err.call_args[0][0]
        self.assertNotIn("While", err_arg)
        self.assertIn("workdir resolution failed", err_arg)

    def test_preflighterror_does_not_show_traceback_expander(self):
        """Curated path should NOT call st.expander -- the hint IS the
        actionable info; the traceback would just confuse the operator."""
        exc = PreflightError("oops", stage="terraform_init")
        with patch.object(error_surface.st, "error"), \
             patch.object(error_surface.st, "info"), \
             patch.object(error_surface.st, "caption"), \
             patch.object(error_surface.st, "expander") as m_exp:
            error_surface.render_error(exc)
        m_exp.assert_not_called()


class RenderErrorGenericTests(unittest.TestCase):
    """Anything that's not a PreflightError -> generic path."""

    def test_runtimeerror_renders_generic_banner_and_traceback(self):
        try:
            raise RuntimeError("upstream blew up")
        except RuntimeError as e:
            exc = e

        m_expander = MagicMock()
        # st.expander returns a context manager; the .__enter__ must
        # return something so the `with` block inside render_error works.
        m_expander.return_value.__enter__ = MagicMock(return_value=None)
        m_expander.return_value.__exit__ = MagicMock(return_value=False)

        with patch.object(error_surface.st, "error") as m_err, \
             patch.object(error_surface.st, "expander", m_expander), \
             patch.object(error_surface.st, "code") as m_code:
            error_surface.render_error(exc, context="doing a thing")

        # Generic banner: should include the type name + the message.
        m_err.assert_called_once()
        err_arg = m_err.call_args[0][0]
        self.assertIn("doing a thing", err_arg)
        self.assertIn("RuntimeError", err_arg)
        self.assertIn("upstream blew up", err_arg)
        self.assertIn("Cloud Logging", err_arg)

        # Expander should fire and its body should be st.code with
        # the formatted traceback.
        m_expander.assert_called_once()
        m_code.assert_called_once()
        code_arg = m_code.call_args[0][0]
        self.assertIn("RuntimeError", code_arg)
        self.assertIn("upstream blew up", code_arg)

    def test_generic_path_does_not_call_info(self):
        """st.info is reserved for the PreflightError hint; generic
        errors shouldn't hijack it."""
        exc = ValueError("oops")
        m_expander = MagicMock()
        m_expander.return_value.__enter__ = MagicMock(return_value=None)
        m_expander.return_value.__exit__ = MagicMock(return_value=False)
        with patch.object(error_surface.st, "error"), \
             patch.object(error_surface.st, "info") as m_info, \
             patch.object(error_surface.st, "expander", m_expander), \
             patch.object(error_surface.st, "code"):
            error_surface.render_error(exc)
        m_info.assert_not_called()


if __name__ == "__main__":
    unittest.main()
