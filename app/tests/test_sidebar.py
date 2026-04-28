# app/tests/test_sidebar.py
"""Unit tests for app.ui.sidebar (PUI-1).

Covers:
  * _list_gcp_projects: cached gcloud invocation, edge cases
    (gcloud missing, non-zero exit, empty stdout, malformed lines).
  * Cache decorator behaviour is NOT tested here -- @st.cache_data
    is Streamlit's; we trust it. We test the underlying function
    via .__wrapped__ to bypass the cache and exercise gcloud paths
    directly.

render_sidebar() itself touches Streamlit's sidebar widgets which
require a live ScriptRunContext to render meaningfully. We don't
assert on its visual output -- the page-level smoke (running the
app and clicking around) is the test surface for the rendering path.
What we DO test is that the cached gcloud call returns the right
shape for the variety of failure modes the sidebar must tolerate
without crashing the whole page.
"""

from __future__ import annotations

import subprocess
import unittest
from unittest.mock import patch, MagicMock

from app.ui import sidebar


class ListGcpProjectsTests(unittest.TestCase):
    """Pin the gcloud-listing helper. Empty list on every failure mode
    so the sidebar can fall back to the text input without raising."""

    def setUp(self):
        # @st.cache_data wraps _list_gcp_projects; access the raw
        # function via .__wrapped__ so each test starts clean (cache
        # state from one test doesn't leak into the next).
        self.fn = sidebar._list_gcp_projects.__wrapped__

    def test_returns_sorted_project_ids(self):
        """Happy path: gcloud returns a newline-separated list."""
        mock_result = MagicMock(
            returncode=0,
            stdout="proj-c\nproj-a\nproj-b\n",
        )
        with patch("subprocess.run", return_value=mock_result):
            result = self.fn()
        self.assertEqual(result, ["proj-a", "proj-b", "proj-c"])

    def test_strips_blank_lines(self):
        """Blank lines mid-output (rare but possible) shouldn't appear
        as empty-string projects in the dropdown."""
        mock_result = MagicMock(
            returncode=0,
            stdout="proj-a\n\nproj-b\n   \n",
        )
        with patch("subprocess.run", return_value=mock_result):
            result = self.fn()
        self.assertEqual(result, ["proj-a", "proj-b"])

    def test_returns_empty_on_non_zero_exit(self):
        """Auth failure / permission denied -> empty list, no raise."""
        mock_result = MagicMock(returncode=1, stdout="", stderr="403")
        with patch("subprocess.run", return_value=mock_result):
            result = self.fn()
        self.assertEqual(result, [])

    def test_returns_empty_on_empty_stdout(self):
        """gcloud succeeded but the SA can't see any projects."""
        mock_result = MagicMock(returncode=0, stdout="")
        with patch("subprocess.run", return_value=mock_result):
            result = self.fn()
        self.assertEqual(result, [])

    def test_returns_empty_on_gcloud_missing(self):
        """gcloud not installed -> FileNotFoundError -> empty list."""
        with patch("subprocess.run", side_effect=FileNotFoundError):
            result = self.fn()
        self.assertEqual(result, [])

    def test_returns_empty_on_timeout(self):
        """Network-hung gcloud -> TimeoutExpired -> empty list.
        10s default keeps the page from blocking on a stuck SDK."""
        with patch(
            "subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="gcloud", timeout=10),
        ):
            result = self.fn()
        self.assertEqual(result, [])

    def test_returns_empty_on_oserror(self):
        """Other OS-level fault (permission, EBUSY, etc.) -> empty list."""
        with patch("subprocess.run", side_effect=OSError("denied")):
            result = self.fn()
        self.assertEqual(result, [])

    def test_filter_passes_active_projects_only(self):
        """The gcloud invocation should filter to active projects --
        otherwise pending-deletion projects clutter the dropdown.
        Pin the filter flag in the args."""
        mock_result = MagicMock(returncode=0, stdout="proj-a\n")
        with patch("subprocess.run", return_value=mock_result) as mock_run:
            self.fn()
        args, _ = mock_run.call_args
        cmd = args[0]
        self.assertIn("--filter=lifecycleState:ACTIVE", cmd)


if __name__ == "__main__":
    unittest.main()
