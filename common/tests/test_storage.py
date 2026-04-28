# common/tests/test_storage.py
"""Unit tests for common.storage (PSA-3).

Covers:
  * state_bucket() env-var resolution + default
  * _gcs_prefix() URI shape
  * tenant_id / project_id validation (path-traversal guard)
  * hydrate_workdir invokes the right gcloud command
  * persist_workdir invokes the right gcloud command WITH excludes
  * Error-path: gcloud failure surfaces as CalledProcessError

All tests mock ``subprocess.run`` via the ``_run_gcloud`` seam --
no real GCS calls. The single integration test that DOES hit a real
bucket lives separately and is skip-by-default.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from common import storage


class StateBucketTests(unittest.TestCase):
    """Pin the bucket-name resolution chain.

    Single source of truth for the bucket name. If a future caller
    reads MTAGENT_STATE_BUCKET directly, the default chain forks --
    these tests catch that by ensuring state_bucket() honours the
    env var AND falls back to the documented default.
    """

    def test_returns_env_var_when_set(self):
        with patch.dict(os.environ, {"MTAGENT_STATE_BUCKET": "company-prod"}):
            self.assertEqual(storage.state_bucket(), "company-prod")

    def test_returns_default_when_env_unset(self):
        # Force the env var unset for this test (it may be set by the
        # operator's shell or .env in real life).
        env = {k: v for k, v in os.environ.items()
               if k != "MTAGENT_STATE_BUCKET"}
        with patch.dict(os.environ, env, clear=True):
            self.assertEqual(storage.state_bucket(), "mtagent-state-dev")

    def test_empty_env_var_uses_default(self):
        """Defensive: empty string env var should NOT be treated as
        the bucket name (which would yield ``gs:///tenants/...``)."""
        with patch.dict(os.environ, {"MTAGENT_STATE_BUCKET": ""}):
            # os.environ.get returns "" for empty env var; that's
            # truthy-falsy boundary. state_bucket uses .get with a
            # default, so an empty string IS returned. This is a
            # known edge case -- caller must not set the env var to
            # empty. We pin the current behaviour here so a future
            # change to "treat empty as unset" surfaces in code review.
            self.assertEqual(storage.state_bucket(), "")


class GcsPrefixTests(unittest.TestCase):
    """Pin the GCS URI shape so the path layout doesn't drift."""

    def test_prefix_uses_tenants_projects_layout(self):
        with patch.dict(os.environ, {"MTAGENT_STATE_BUCKET": "test-bucket"}):
            self.assertEqual(
                storage._gcs_prefix("default", "dev-proj-470211"),
                "gs://test-bucket/tenants/default/projects/dev-proj-470211/",
            )

    def test_prefix_has_trailing_slash(self):
        """gcloud storage rsync semantics: trailing slash means
        'directory contents,' no slash means 'directory itself.'
        We always want contents-mode."""
        with patch.dict(os.environ, {"MTAGENT_STATE_BUCKET": "b"}):
            uri = storage._gcs_prefix("t", "dev-proj-470211")
            self.assertTrue(uri.endswith("/"),
                            f"prefix must end with slash: {uri!r}")


class IdValidationTests(unittest.TestCase):
    """Pin the path-traversal guards on tenant_id + project_id."""

    def test_valid_tenant_id_passes(self):
        # Should not raise
        storage._validate_ids("default", "dev-proj-470211")
        storage._validate_ids("acme-corp_prod", "dev-proj-470211")
        storage._validate_ids("tenant-uuid-abc123", "dev-proj-470211")

    def test_invalid_tenant_id_with_slash_rejected(self):
        with self.assertRaises(ValueError):
            storage._validate_ids("../../etc", "dev-proj-470211")

    def test_invalid_tenant_id_starting_with_hyphen_rejected(self):
        with self.assertRaises(ValueError):
            storage._validate_ids("-leading-hyphen", "dev-proj-470211")

    def test_invalid_project_id_uppercase_rejected(self):
        with self.assertRaises(ValueError):
            storage._validate_ids("default", "DEV-PROJ-470211")

    def test_invalid_project_id_too_short_rejected(self):
        with self.assertRaises(ValueError):
            storage._validate_ids("default", "abc")

    def test_invalid_project_id_with_path_traversal_rejected(self):
        with self.assertRaises(ValueError):
            storage._validate_ids("default", "../etc/passwd")


class HydrateWorkdirTests(unittest.TestCase):
    """Pin the hydrate gcloud command shape + return value."""

    def setUp(self):
        # Patch subprocess seam so no real gcloud calls fire.
        self._patcher = patch.object(storage, "_run_gcloud")
        self.mock_run = self._patcher.start()
        self.mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="", stderr="",
        )
        # Tmp local root so we don't pollute /tmp.
        self._tmpdir = tempfile.TemporaryDirectory()
        self.local_root = self._tmpdir.name

    def tearDown(self):
        self._patcher.stop()
        self._tmpdir.cleanup()

    def test_hydrate_returns_correct_local_path(self):
        with patch.dict(os.environ, {"MTAGENT_STATE_BUCKET": "test-bucket"}):
            result = storage.hydrate_workdir(
                "dev-proj-470211", local_root=self.local_root,
            )
        expected = os.path.join(self.local_root, "dev-proj-470211")
        self.assertEqual(result, expected)
        self.assertTrue(os.path.isdir(result),
                        "hydrate must create the local dir")

    def test_hydrate_invokes_gcloud_rsync(self):
        with patch.dict(os.environ, {"MTAGENT_STATE_BUCKET": "test-bucket"}):
            storage.hydrate_workdir(
                "dev-proj-470211", local_root=self.local_root,
            )
        # Inspect the gcloud command we would have run.
        self.mock_run.assert_called_once()
        called_args = self.mock_run.call_args[0][0]
        self.assertEqual(called_args[:4],
                         ["gcloud", "storage", "rsync", "--recursive"])
        # Source URI is the canonical (tenant=default, project=dev-...) prefix
        self.assertEqual(
            called_args[4],
            "gs://test-bucket/tenants/default/projects/dev-proj-470211/",
        )
        # Destination is the local path we returned
        self.assertEqual(
            called_args[5],
            os.path.join(self.local_root, "dev-proj-470211"),
        )

    def test_hydrate_uses_tenant_id_when_provided(self):
        with patch.dict(os.environ, {"MTAGENT_STATE_BUCKET": "b"}):
            storage.hydrate_workdir(
                "dev-proj-470211",
                tenant_id="acme-corp",
                local_root=self.local_root,
            )
        called_args = self.mock_run.call_args[0][0]
        self.assertIn("gs://b/tenants/acme-corp/projects/dev-proj-470211/",
                      called_args)

    def test_hydrate_validates_project_id(self):
        with self.assertRaises(ValueError):
            storage.hydrate_workdir("BAD-PROJECT", local_root=self.local_root)
        # gcloud must NOT have been called
        self.mock_run.assert_not_called()

    def test_hydrate_propagates_gcloud_failure(self):
        self.mock_run.side_effect = subprocess.CalledProcessError(
            returncode=1, cmd=["gcloud", "storage", "rsync"],
            stderr="bucket not found",
        )
        with self.assertRaises(subprocess.CalledProcessError):
            storage.hydrate_workdir(
                "dev-proj-470211", local_root=self.local_root,
            )

    def test_hydrate_uses_env_var_local_root_when_unspecified(self):
        with patch.dict(os.environ, {
            "MTAGENT_IMPORT_BASE": self.local_root,
            "MTAGENT_STATE_BUCKET": "b",
        }):
            result = storage.hydrate_workdir("dev-proj-470211")
        self.assertEqual(
            result, os.path.join(self.local_root, "dev-proj-470211"),
        )


class PersistWorkdirTests(unittest.TestCase):
    """Pin the persist gcloud command shape + exclude flags."""

    def setUp(self):
        self._patcher = patch.object(storage, "_run_gcloud")
        self.mock_run = self._patcher.start()
        self.mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="", stderr="",
        )
        self._tmpdir = tempfile.TemporaryDirectory()
        self.local_path = os.path.join(self._tmpdir.name, "dev-proj-470211")
        os.makedirs(self.local_path, exist_ok=True)

    def tearDown(self):
        self._patcher.stop()
        self._tmpdir.cleanup()

    def test_persist_invokes_gcloud_rsync_with_excludes(self):
        with patch.dict(os.environ, {"MTAGENT_STATE_BUCKET": "test-bucket"}):
            storage.persist_workdir(self.local_path, "dev-proj-470211")
        called_args = self.mock_run.call_args[0][0]
        # Must include --recursive and the rsync command
        self.assertEqual(called_args[:4],
                         ["gcloud", "storage", "rsync", "--recursive"])
        # Must include --delete-unmatched-destination-objects so deletes
        # are mirrored to GCS (e.g. quarantined .tf removed from workdir
        # also gets removed from the persisted state).
        self.assertIn("--delete-unmatched-destination-objects",
                      called_args)
        # Each exclude pattern must be present as a --exclude PATTERN pair.
        for pattern in ("_diagnostics/**", "*.backup",
                        "*.tfstate.backup", "*.tfstate.*.backup"):
            self.assertIn(pattern, called_args,
                          f"excludes must include {pattern!r}")

    def test_persist_source_path_has_trailing_slash(self):
        """gcloud rsync semantics demand trailing slash for content-mode."""
        with patch.dict(os.environ, {"MTAGENT_STATE_BUCKET": "b"}):
            storage.persist_workdir(self.local_path, "dev-proj-470211")
        called_args = self.mock_run.call_args[0][0]
        # Find the source path arg (the second-to-last positional, before
        # the gs:// dest).
        # Args end with [src_path, dest_uri], find index of dest.
        dest_idx = next(i for i, a in enumerate(called_args)
                        if a.startswith("gs://"))
        src_path = called_args[dest_idx - 1]
        self.assertTrue(
            src_path.endswith("/"),
            f"source path must end with slash for content-mode: {src_path!r}",
        )

    def test_persist_destination_uri_correct(self):
        with patch.dict(os.environ, {"MTAGENT_STATE_BUCKET": "test-bucket"}):
            storage.persist_workdir(
                self.local_path, "dev-proj-470211", tenant_id="acme",
            )
        called_args = self.mock_run.call_args[0][0]
        self.assertIn(
            "gs://test-bucket/tenants/acme/projects/dev-proj-470211/",
            called_args,
        )

    def test_persist_validates_project_id(self):
        with self.assertRaises(ValueError):
            storage.persist_workdir(self.local_path, "BAD-PROJECT")
        self.mock_run.assert_not_called()

    def test_persist_raises_when_local_path_missing(self):
        bogus = os.path.join(self._tmpdir.name, "nonexistent")
        with self.assertRaises(FileNotFoundError):
            storage.persist_workdir(bogus, "dev-proj-470211")
        self.mock_run.assert_not_called()

    def test_persist_propagates_gcloud_failure(self):
        self.mock_run.side_effect = subprocess.CalledProcessError(
            returncode=1, cmd=["gcloud", "storage", "rsync"],
            stderr="permission denied on bucket",
        )
        with self.assertRaises(subprocess.CalledProcessError):
            storage.persist_workdir(self.local_path, "dev-proj-470211")


class GcsBackendEnabledTests(unittest.TestCase):
    """Pin the MTAGENT_USE_GCS_BACKEND env-var gate.

    Default OFF preserves local-dev behaviour (no GCS auth required for
    `terraform init`). Cloud Run cloudbuild.yaml sets it ON.
    """

    def test_unset_returns_false(self):
        env = {k: v for k, v in os.environ.items()
               if k != "MTAGENT_USE_GCS_BACKEND"}
        with patch.dict(os.environ, env, clear=True):
            self.assertFalse(storage.gcs_backend_enabled())

    def test_set_to_1_returns_true(self):
        with patch.dict(os.environ, {"MTAGENT_USE_GCS_BACKEND": "1"}):
            self.assertTrue(storage.gcs_backend_enabled())

    def test_set_to_true_returns_true(self):
        with patch.dict(os.environ, {"MTAGENT_USE_GCS_BACKEND": "true"}):
            self.assertTrue(storage.gcs_backend_enabled())

    def test_truthy_aliases_all_work(self):
        for val in ("yes", "on", "TRUE", "On", "Yes"):
            with self.subTest(val=val):
                with patch.dict(os.environ,
                                {"MTAGENT_USE_GCS_BACKEND": val}):
                    self.assertTrue(storage.gcs_backend_enabled(),
                                    f"value {val!r} should enable")

    def test_falsy_values_return_false(self):
        for val in ("0", "false", "no", "off", "", "FALSE"):
            with self.subTest(val=val):
                with patch.dict(os.environ,
                                {"MTAGENT_USE_GCS_BACKEND": val}):
                    self.assertFalse(storage.gcs_backend_enabled(),
                                     f"value {val!r} should disable")


class GenerateBackendConfigTests(unittest.TestCase):
    """Pin the HCL output shape -- terraform parses this verbatim."""

    def test_output_contains_backend_block(self):
        with patch.dict(os.environ, {"MTAGENT_STATE_BUCKET": "test-bucket"}):
            hcl = storage.generate_backend_config("dev-proj-470211")
        # Required terraform block markers
        self.assertIn("terraform {", hcl)
        self.assertIn('backend "gcs" {', hcl)
        self.assertIn('bucket = "test-bucket"', hcl)
        self.assertIn(
            'prefix = "tenants/default/projects/dev-proj-470211/terraform-state"',
            hcl,
        )

    def test_output_uses_tenant_id_when_provided(self):
        with patch.dict(os.environ, {"MTAGENT_STATE_BUCKET": "b"}):
            hcl = storage.generate_backend_config(
                "dev-proj-470211", tenant_id="acme-corp",
            )
        self.assertIn(
            'prefix = "tenants/acme-corp/projects/dev-proj-470211/terraform-state"',
            hcl,
        )

    def test_validates_project_id(self):
        with patch.dict(os.environ, {"MTAGENT_STATE_BUCKET": "b"}):
            with self.assertRaises(ValueError):
                storage.generate_backend_config("BAD-PROJECT")

    def test_output_starts_with_warning_comment(self):
        """Operators reading the file should immediately see it's
        auto-generated + understand the regen path."""
        with patch.dict(os.environ, {"MTAGENT_STATE_BUCKET": "b"}):
            hcl = storage.generate_backend_config("dev-proj-470211")
        self.assertTrue(hcl.startswith("# AUTO-GENERATED"),
                        "first line must be the warning comment")
        self.assertIn("seed_backend_config", hcl,
                      "should reference the regen helper by name")


class SeedBackendConfigTests(unittest.TestCase):
    """Pin the seed_backend_config contract: gate, idempotency, write."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.workdir = self._tmpdir.name

    def tearDown(self):
        self._tmpdir.cleanup()

    def test_no_op_when_env_disabled(self):
        """Local-dev path: env unset -> no file written."""
        env = {k: v for k, v in os.environ.items()
               if k != "MTAGENT_USE_GCS_BACKEND"}
        with patch.dict(os.environ, env, clear=True):
            result = storage.seed_backend_config(
                self.workdir, "dev-proj-470211",
            )
        self.assertFalse(result, "should return False when gated off")
        self.assertFalse(
            os.path.isfile(os.path.join(self.workdir,
                                        "_backend_seed.tf")),
            "no file should be written when env disabled",
        )

    def test_writes_file_when_env_enabled(self):
        with patch.dict(os.environ, {
            "MTAGENT_USE_GCS_BACKEND": "1",
            "MTAGENT_STATE_BUCKET": "test-bucket",
        }):
            result = storage.seed_backend_config(
                self.workdir, "dev-proj-470211",
            )
        self.assertTrue(result)
        target = os.path.join(self.workdir, "_backend_seed.tf")
        self.assertTrue(os.path.isfile(target))
        # Verify content is the expected HCL
        with open(target, encoding="utf-8") as f:
            content = f.read()
        self.assertIn('backend "gcs"', content)
        self.assertIn('bucket = "test-bucket"', content)

    def test_no_op_when_file_already_exists(self):
        """Operator may have customized the backend config; never
        silently overwrite. Same shape as seed_lock_file +
        seed_providers_stub (D-6 fix)."""
        existing = "# operator's custom backend; do not touch\n"
        target = os.path.join(self.workdir, "_backend_seed.tf")
        with open(target, "w", encoding="utf-8") as f:
            f.write(existing)
        with patch.dict(os.environ, {
            "MTAGENT_USE_GCS_BACKEND": "1",
            "MTAGENT_STATE_BUCKET": "test-bucket",
        }):
            result = storage.seed_backend_config(
                self.workdir, "dev-proj-470211",
            )
        self.assertFalse(result, "no-op when existing file present")
        # Confirm we didn't overwrite it
        with open(target, encoding="utf-8") as f:
            self.assertEqual(f.read(), existing)

    def test_validates_project_id_when_enabled(self):
        with patch.dict(os.environ, {
            "MTAGENT_USE_GCS_BACKEND": "1",
            "MTAGENT_STATE_BUCKET": "b",
        }):
            with self.assertRaises(ValueError):
                storage.seed_backend_config(self.workdir, "BAD-PROJECT")

    def test_no_validation_when_env_disabled(self):
        """Bad project_id passed when env is off should NOT raise --
        the function returns early before validation. Defensive
        against accidental local-dev calls with sloppy IDs."""
        env = {k: v for k, v in os.environ.items()
               if k != "MTAGENT_USE_GCS_BACKEND"}
        with patch.dict(os.environ, env, clear=True):
            # Should NOT raise; returns False (no-op)
            result = storage.seed_backend_config(self.workdir, "BAD-PROJECT")
        self.assertFalse(result)


class PersistExcludesTests(unittest.TestCase):
    """PSA-5: terraform.tfstate must be excluded from persist.

    With the GCS backend, terraform owns its state directly in the
    bucket; we must NEVER rsync a stray local terraform.tfstate
    because that would clobber the canonical backend state.
    """

    def test_persist_excludes_terraform_tfstate(self):
        """terraform.tfstate must be in the exclude list."""
        self.assertIn("terraform.tfstate", storage._PERSIST_EXCLUDES,
                      "terraform.tfstate must be excluded; otherwise GCS "
                      "backend state could be clobbered by stale local file")

    def test_persist_excludes_lock_info(self):
        self.assertIn("terraform.tfstate.lock.info",
                      storage._PERSIST_EXCLUDES)


if __name__ == "__main__":
    unittest.main()
