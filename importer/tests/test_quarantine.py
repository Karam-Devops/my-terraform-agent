# importer/tests/test_quarantine.py
"""CG-7 tests for importer.quarantine.

Covers:
  * is_auto_quarantine_enabled() env var parsing
  * quarantine_path() pure path computation
  * quarantine_resource() happy path + failure modes
    (missing source file, state_rm failure with revert, partial
    failure handling)

terraform_client.state_rm is mocked so tests don't shell out to real
terraform. The quarantine module's contract is that it ONLY does file
I/O + delegates state mutation to terraform_client.state_rm; tests
verify both halves and the revert-on-state-rm-failure path.
"""

from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from unittest.mock import patch


class IsAutoQuarantineEnabledTests(unittest.TestCase):
    """Pin the env-var parsing rules so future maintainers can't
    silently change "1 means on" without breaking the test."""

    def test_unset_returns_false(self):
        from importer.quarantine import is_auto_quarantine_enabled
        with patch.dict(os.environ, {}, clear=True):
            self.assertFalse(is_auto_quarantine_enabled())

    def test_truthy_values_return_true(self):
        from importer.quarantine import is_auto_quarantine_enabled
        for val in ("1", "true", "True", "TRUE", "yes", "YES", "on", "ON"):
            with self.subTest(value=val):
                with patch.dict(os.environ,
                                {"IMPORTER_AUTO_QUARANTINE": val}):
                    self.assertTrue(
                        is_auto_quarantine_enabled(),
                        f"Expected '{val}' to be truthy",
                    )

    def test_falsy_values_return_false(self):
        from importer.quarantine import is_auto_quarantine_enabled
        for val in ("0", "false", "no", "off", "", "  ", "anything-else"):
            with self.subTest(value=val):
                with patch.dict(os.environ,
                                {"IMPORTER_AUTO_QUARANTINE": val}):
                    self.assertFalse(
                        is_auto_quarantine_enabled(),
                        f"Expected '{val}' to be falsy",
                    )


class QuarantinePathTests(unittest.TestCase):
    def test_returns_workdir_subdir(self):
        from importer.quarantine import quarantine_path, QUARANTINE_DIRNAME
        self.assertEqual(
            quarantine_path("/some/workdir"),
            os.path.join("/some/workdir", QUARANTINE_DIRNAME),
        )

    def test_pure_function_does_not_create_dir(self):
        # Path computation MUST NOT create the directory -- caller
        # creates it lazily on first quarantine event.
        from importer.quarantine import quarantine_path
        with tempfile.TemporaryDirectory() as tmp:
            qpath = quarantine_path(tmp)
            self.assertFalse(os.path.exists(qpath))


class QuarantineResourceHappyPathTests(unittest.TestCase):
    def setUp(self):
        self.workdir = tempfile.mkdtemp(prefix="quarantine_test_")
        self.tf_filename = "google_cloud_run_v2_service_poc_cloudrun.tf"
        self.tf_path = os.path.join(self.workdir, self.tf_filename)
        with open(self.tf_path, "w", encoding="utf-8") as f:
            f.write('resource "google_cloud_run_v2_service" "poc_cloudrun" {}\n')

    def tearDown(self):
        shutil.rmtree(self.workdir, ignore_errors=True)

    def test_moves_file_and_runs_state_rm(self):
        from importer.quarantine import quarantine_resource, quarantine_path
        with patch("importer.quarantine.terraform_client.state_rm",
                   return_value=True) as mock_state_rm:
            ok = quarantine_resource(
                workdir=self.workdir,
                tf_address="google_cloud_run_v2_service.poc_cloudrun",
                hcl_filename=self.tf_filename,
                reason="test reason",
            )
        self.assertTrue(ok)
        # File moved.
        self.assertFalse(os.path.exists(self.tf_path))
        moved = os.path.join(quarantine_path(self.workdir), self.tf_filename)
        self.assertTrue(os.path.isfile(moved))
        # state rm called with correct args.
        mock_state_rm.assert_called_once_with(
            "google_cloud_run_v2_service.poc_cloudrun",
            workdir=self.workdir,
        )

    def test_writes_reason_sidecar_file(self):
        from importer.quarantine import quarantine_resource, quarantine_path
        with patch("importer.quarantine.terraform_client.state_rm",
                   return_value=True):
            quarantine_resource(
                workdir=self.workdir,
                tf_address="google_cloud_run_v2_service.poc_cloudrun",
                hcl_filename=self.tf_filename,
                reason="startup_cpu_boost is not expected here",
            )
        sidecar = os.path.join(
            quarantine_path(self.workdir),
            self.tf_filename + ".quarantine.txt",
        )
        self.assertTrue(os.path.isfile(sidecar))
        with open(sidecar, "r", encoding="utf-8") as f:
            content = f.read()
        # Sidecar carries the reason so the dir is self-documenting.
        self.assertIn("startup_cpu_boost is not expected here", content)
        self.assertIn("google_cloud_run_v2_service.poc_cloudrun", content)


class QuarantineResourceFailureModeTests(unittest.TestCase):
    def setUp(self):
        self.workdir = tempfile.mkdtemp(prefix="quarantine_fail_")

    def tearDown(self):
        shutil.rmtree(self.workdir, ignore_errors=True)

    def test_missing_source_file_returns_false(self):
        # Defensive: caller passed a filename that doesn't exist on
        # disk. Don't create the quarantine dir (no-op), return False.
        from importer.quarantine import quarantine_resource, quarantine_path
        ok = quarantine_resource(
            workdir=self.workdir,
            tf_address="x.y",
            hcl_filename="nonexistent.tf",
        )
        self.assertFalse(ok)
        # Quarantine dir should NOT have been created.
        self.assertFalse(os.path.exists(quarantine_path(self.workdir)))

    def test_state_rm_failure_reverts_file_move(self):
        # When state_rm fails, the .tf file MUST be moved back so
        # the workdir + state stay consistent. Otherwise we'd have
        # a state entry for a resource whose .tf is in quarantine,
        # which would cause "destroy" on next plan.
        from importer.quarantine import quarantine_resource
        tf_filename = "test.tf"
        tf_path = os.path.join(self.workdir, tf_filename)
        with open(tf_path, "w", encoding="utf-8") as f:
            f.write("# original")

        with patch("importer.quarantine.terraform_client.state_rm",
                   return_value=False):
            ok = quarantine_resource(
                workdir=self.workdir,
                tf_address="x.y",
                hcl_filename=tf_filename,
            )

        self.assertFalse(ok)
        # File should be back where it started.
        self.assertTrue(os.path.isfile(tf_path),
                        "Source file was not reverted after state_rm failure")
        # Original content preserved.
        with open(tf_path, "r", encoding="utf-8") as f:
            self.assertEqual(f.read(), "# original")


if __name__ == "__main__":
    unittest.main()
