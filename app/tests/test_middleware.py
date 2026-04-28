# app/tests/test_middleware.py
"""Unit tests for app.middleware (PSA-4).

Covers:
  * workdir_context happy path: hydrate -> yield -> persist
  * Cache-hit path: reuse hydrated workdir, no re-hydrate
  * Exception in body: persist NOT called (preserve previous-good)
  * persist_on_exit=False: persist NOT called
  * MTAGENT_IMPORT_BASE env var: set on enter, restored on exit
  * cleanup_session_workdirs: removes /tmp dirs, logs failures

All tests mock common.storage.hydrate_workdir + persist_workdir at
the seam so no real GCS calls fire. Module-level WorkdirSession is
reset before each test for isolation.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from unittest.mock import patch, MagicMock

from app import middleware


class WorkdirContextHappyPathTests(unittest.TestCase):
    """Pin the standard hydrate -> yield -> persist flow."""

    def setUp(self):
        middleware._reset_module_session()
        self._tmpdir = tempfile.TemporaryDirectory()
        self.tmp_root = self._tmpdir.name

    def tearDown(self):
        self._tmpdir.cleanup()
        # Restore env in case a test leaked it
        os.environ.pop("MTAGENT_IMPORT_BASE", None)

    @patch("app.middleware.persist_workdir")
    @patch("app.middleware.hydrate_workdir")
    def test_hydrate_called_on_enter(self, mock_hydrate, mock_persist):
        mock_hydrate.return_value = "/tmp/imported/abc12345/dev-proj-470211"
        with middleware.workdir_context("dev-proj-470211") as wd:
            mock_hydrate.assert_called_once()
            self.assertEqual(wd, "/tmp/imported/abc12345/dev-proj-470211")

    @patch("app.middleware.persist_workdir")
    @patch("app.middleware.hydrate_workdir")
    def test_persist_called_on_clean_exit(self, mock_hydrate, mock_persist):
        mock_hydrate.return_value = "/tmp/imported/abc12345/dev-proj-470211"
        with middleware.workdir_context("dev-proj-470211"):
            pass
        mock_persist.assert_called_once()
        # Persist receives the hydrated local path
        called_args = mock_persist.call_args
        self.assertEqual(
            called_args[0][0],
            "/tmp/imported/abc12345/dev-proj-470211",
        )
        self.assertEqual(called_args[0][1], "dev-proj-470211")

    @patch("app.middleware.persist_workdir")
    @patch("app.middleware.hydrate_workdir")
    def test_tenant_id_propagates(self, mock_hydrate, mock_persist):
        mock_hydrate.return_value = "/tmp/imported/abc/dev-proj-470211"
        with middleware.workdir_context(
            "dev-proj-470211", tenant_id="acme-corp",
        ):
            pass
        # Both calls receive the tenant_id
        hydrate_kwargs = mock_hydrate.call_args[1]
        self.assertEqual(hydrate_kwargs["tenant_id"], "acme-corp")
        persist_kwargs = mock_persist.call_args[1]
        self.assertEqual(persist_kwargs["tenant_id"], "acme-corp")

    @patch("app.middleware.persist_workdir")
    @patch("app.middleware.hydrate_workdir")
    def test_request_uuid_in_local_root(self, mock_hydrate, mock_persist):
        """The middleware must pass an 8-hex-char UUID-scoped local
        root to hydrate_workdir so engines find it via
        MTAGENT_IMPORT_BASE."""
        mock_hydrate.return_value = "/tmp/imported/abc/dev-proj-470211"
        with middleware.workdir_context("dev-proj-470211"):
            pass
        local_root = mock_hydrate.call_args[1]["local_root"]
        # Format: /tmp/imported/<8-hex>
        self.assertTrue(
            local_root.startswith("/tmp/imported/"),
            f"local_root prefix wrong: {local_root!r}",
        )
        suffix = local_root[len("/tmp/imported/"):]
        self.assertEqual(
            len(suffix), 8,
            f"UUID suffix should be 8 chars, got {suffix!r}",
        )
        self.assertTrue(
            all(c in "0123456789abcdef" for c in suffix),
            f"UUID suffix should be hex, got {suffix!r}",
        )


class WorkdirContextCacheTests(unittest.TestCase):
    """Pin the per-session workdir caching behaviour."""

    def setUp(self):
        middleware._reset_module_session()

    def tearDown(self):
        os.environ.pop("MTAGENT_IMPORT_BASE", None)

    @patch("app.middleware.persist_workdir")
    @patch("app.middleware.hydrate_workdir")
    def test_second_call_same_project_reuses_workdir(
        self, mock_hydrate, mock_persist,
    ):
        """The whole point of the session cache: don't re-hydrate the
        ~150MB .terraform/providers blob on every UI interaction."""
        mock_hydrate.return_value = "/tmp/imported/abc/dev-proj-470211"
        with middleware.workdir_context("dev-proj-470211") as wd1:
            pass
        with middleware.workdir_context("dev-proj-470211") as wd2:
            pass
        # Hydrate called ONCE
        self.assertEqual(mock_hydrate.call_count, 1)
        # Both yielded the same path
        self.assertEqual(wd1, wd2)
        # Persist called for BOTH (every action persists; cheap rsync)
        self.assertEqual(mock_persist.call_count, 2)

    @patch("app.middleware.persist_workdir")
    @patch("app.middleware.hydrate_workdir")
    def test_different_projects_get_different_workdirs(
        self, mock_hydrate, mock_persist,
    ):
        """Each (tenant, project) gets its own UUID-scoped workdir."""
        mock_hydrate.side_effect = [
            "/tmp/imported/uuid1/dev-proj-470211",
            "/tmp/imported/uuid2/prod-proj-987654",
        ]
        with middleware.workdir_context("dev-proj-470211") as wd1:
            pass
        with middleware.workdir_context("prod-proj-987654") as wd2:
            pass
        self.assertEqual(mock_hydrate.call_count, 2)
        self.assertNotEqual(wd1, wd2)

    @patch("app.middleware.persist_workdir")
    @patch("app.middleware.hydrate_workdir")
    def test_different_tenants_same_project_get_different_workdirs(
        self, mock_hydrate, mock_persist,
    ):
        """Multi-tenant safety: tenant A's dev-proj-470211 must not
        share a workdir with tenant B's dev-proj-470211."""
        mock_hydrate.side_effect = [
            "/tmp/imported/uuid1/dev-proj-470211",
            "/tmp/imported/uuid2/dev-proj-470211",
        ]
        with middleware.workdir_context(
            "dev-proj-470211", tenant_id="tenant-a",
        ) as wd1:
            pass
        with middleware.workdir_context(
            "dev-proj-470211", tenant_id="tenant-b",
        ) as wd2:
            pass
        self.assertEqual(mock_hydrate.call_count, 2)
        self.assertNotEqual(wd1, wd2)


class WorkdirContextErrorHandlingTests(unittest.TestCase):
    """Pin the persist-skip-on-error contract + env restoration."""

    def setUp(self):
        middleware._reset_module_session()

    def tearDown(self):
        os.environ.pop("MTAGENT_IMPORT_BASE", None)

    @patch("app.middleware.persist_workdir")
    @patch("app.middleware.hydrate_workdir")
    def test_exception_in_body_skips_persist(
        self, mock_hydrate, mock_persist,
    ):
        """Critical contract: if engine code raises, DON'T persist
        the (potentially corrupt) local state. Customer's previous-
        good GCS state is preserved for next request to re-hydrate."""
        mock_hydrate.return_value = "/tmp/imported/abc/dev-proj-470211"
        with self.assertRaises(RuntimeError):
            with middleware.workdir_context("dev-proj-470211"):
                raise RuntimeError("simulated engine failure")
        mock_persist.assert_not_called()

    @patch("app.middleware.persist_workdir")
    @patch("app.middleware.hydrate_workdir")
    def test_persist_on_exit_false_skips_persist(
        self, mock_hydrate, mock_persist,
    ):
        """Read-only operations can opt out of persist."""
        mock_hydrate.return_value = "/tmp/imported/abc/dev-proj-470211"
        with middleware.workdir_context(
            "dev-proj-470211", persist_on_exit=False,
        ):
            pass
        mock_persist.assert_not_called()

    @patch("app.middleware.persist_workdir")
    @patch("app.middleware.hydrate_workdir")
    def test_hydrate_failure_propagates_and_restores_env(
        self, mock_hydrate, mock_persist,
    ):
        """If hydrate itself fails, env must NOT leak to the caller."""
        os.environ["MTAGENT_IMPORT_BASE"] = "/original/base"
        mock_hydrate.side_effect = RuntimeError("gcloud not auth'd")
        with self.assertRaises(RuntimeError):
            with middleware.workdir_context("dev-proj-470211"):
                pass
        self.assertEqual(
            os.environ.get("MTAGENT_IMPORT_BASE"), "/original/base",
            "env must be restored even on hydrate failure",
        )

    @patch("app.middleware.persist_workdir")
    @patch("app.middleware.hydrate_workdir")
    def test_env_var_restored_after_clean_exit(
        self, mock_hydrate, mock_persist,
    ):
        """MTAGENT_IMPORT_BASE must be set during the with-block AND
        restored after."""
        os.environ["MTAGENT_IMPORT_BASE"] = "/original/base"
        mock_hydrate.return_value = "/tmp/imported/abc/dev-proj-470211"
        captured_in_block = None
        with middleware.workdir_context("dev-proj-470211"):
            captured_in_block = os.environ.get("MTAGENT_IMPORT_BASE")
        # Inside the block, env is set to the request-scoped path
        self.assertTrue(
            captured_in_block and captured_in_block.startswith("/tmp/imported/"),
            f"env not set inside block: {captured_in_block!r}",
        )
        # After the block, original value restored
        self.assertEqual(
            os.environ.get("MTAGENT_IMPORT_BASE"), "/original/base",
        )

    @patch("app.middleware.persist_workdir")
    @patch("app.middleware.hydrate_workdir")
    def test_env_var_unset_when_unset_originally(
        self, mock_hydrate, mock_persist,
    ):
        """If MTAGENT_IMPORT_BASE was unset, exit must remove (not set
        to empty) so callers see the same env they started with."""
        os.environ.pop("MTAGENT_IMPORT_BASE", None)
        mock_hydrate.return_value = "/tmp/imported/abc/dev-proj-470211"
        with middleware.workdir_context("dev-proj-470211"):
            pass
        self.assertNotIn("MTAGENT_IMPORT_BASE", os.environ)


class CleanupSessionWorkdirsTests(unittest.TestCase):
    """Pin cleanup behaviour (best-effort + log failures)."""

    def setUp(self):
        middleware._reset_module_session()
        self._tmpdir = tempfile.TemporaryDirectory()
        self.base = self._tmpdir.name

    def tearDown(self):
        self._tmpdir.cleanup()

    @patch("app.middleware.persist_workdir")
    @patch("app.middleware.hydrate_workdir")
    def test_cleanup_removes_local_dirs(self, mock_hydrate, mock_persist):
        # Create a real /tmp-style dir we can rmtree
        request_dir = os.path.join(self.base, "abc12345")
        local_path = os.path.join(request_dir, "dev-proj-470211")
        os.makedirs(local_path)
        mock_hydrate.return_value = local_path

        with middleware.workdir_context("dev-proj-470211"):
            pass

        # Pre-cleanup: dir exists
        self.assertTrue(os.path.isdir(request_dir))

        middleware.cleanup_session_workdirs()

        # Post-cleanup: dir gone
        self.assertFalse(os.path.isdir(request_dir))

    @patch("app.middleware.persist_workdir")
    @patch("app.middleware.hydrate_workdir")
    def test_cleanup_clears_session_handles(
        self, mock_hydrate, mock_persist,
    ):
        local_path = os.path.join(self.base, "abc", "dev-proj-470211")
        os.makedirs(local_path)
        mock_hydrate.return_value = local_path

        with middleware.workdir_context("dev-proj-470211"):
            pass

        session = middleware._get_session()
        self.assertEqual(len(session.handles), 1)

        middleware.cleanup_session_workdirs()

        self.assertEqual(len(session.handles), 0)

    def test_cleanup_no_op_on_empty_session(self):
        """Empty session should not raise."""
        # Should be a clean no-op
        middleware.cleanup_session_workdirs()  # no exception

    @patch("app.middleware._log")
    @patch("app.middleware.persist_workdir")
    @patch("app.middleware.hydrate_workdir")
    def test_cleanup_swallows_rmtree_errors(
        self, mock_hydrate, mock_persist, mock_log,
    ):
        """If rmtree fails (perm denied etc.), log + continue."""
        local_path = os.path.join(self.base, "abc", "dev-proj-470211")
        os.makedirs(local_path)
        mock_hydrate.return_value = local_path

        with middleware.workdir_context("dev-proj-470211"):
            pass

        with patch("app.middleware.shutil.rmtree",
                   side_effect=OSError("simulated permission denied")):
            # Should NOT raise
            middleware.cleanup_session_workdirs()

        # But should have logged the warning
        mock_log.warning.assert_called_once()
        log_event = mock_log.warning.call_args[0][0]
        self.assertEqual(log_event, "workdir_cleanup_failed")


if __name__ == "__main__":
    unittest.main()
