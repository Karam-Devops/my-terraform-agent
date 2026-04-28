# common/tests/test_storage.py
"""Unit tests for common.storage (PSA-3 / PUI-1 SMOKE layer 5 SDK rewrite).

Covers:
  * state_bucket() env-var resolution + default
  * _gcs_prefix() URI shape
  * tenant_id / project_id validation (path-traversal guard)
  * hydrate_workdir downloads blobs via SDK
  * persist_workdir uploads via SDK + applies _PERSIST_EXCLUDES
  * First-run-for-this-project case: empty prefix returns clean
  * Genuine failures (auth, network) propagate

All tests mock ``_get_gcs_client`` so no real GCS calls fire. The
single integration test that DOES hit a real bucket lives separately
and is skip-by-default.

Migration note (PUI-1 SMOKE 2026-04-28): the prior test suite mocked
``subprocess.run`` via ``_run_gcloud`` seam. The SDK rewrite (driven by
gcloud CLI's broken auth chain in Cloud Run) replaced that with
``_get_gcs_client`` -- mocks updated in lockstep.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from unittest.mock import patch, MagicMock

from common import storage
# google.api_core.exceptions stubbed via common/tests/conftest.py when
# the real SDK isn't installed locally.
from google.api_core import exceptions as gcs_exceptions


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
    """Pin hydrate's SDK invocation shape + return value."""

    def setUp(self):
        # Patch the SDK-client seam so no real GCS traffic fires.
        self._patcher = patch.object(storage, "_get_gcs_client")
        self.mock_get_client = self._patcher.start()
        self.mock_client = MagicMock(name="gcs_client")
        self.mock_get_client.return_value = self.mock_client
        # Default: empty list_blobs (= first-run-for-this-project case;
        # individual tests override to exercise download paths).
        self.mock_client.list_blobs.return_value = iter([])

        self._tmpdir = tempfile.TemporaryDirectory()
        self.local_root = self._tmpdir.name

    def tearDown(self):
        self._patcher.stop()
        self._tmpdir.cleanup()

    def _make_blob(self, name: str, fake_content: bytes = b""):
        """Build a MagicMock Blob whose download_to_filename writes
        fake_content to the destination path. Lets tests verify what
        ended up on disk after hydrate."""
        blob = MagicMock(name=f"blob:{name}")
        blob.name = name

        def _fake_download(dest_path: str) -> None:
            with open(dest_path, "wb") as f:
                f.write(fake_content)
        blob.download_to_filename.side_effect = _fake_download
        return blob

    def test_hydrate_returns_correct_local_path(self):
        """Empty list_blobs (first-run path); local dir still created."""
        with patch.dict(os.environ, {"MTAGENT_STATE_BUCKET": "test-bucket"}):
            result = storage.hydrate_workdir(
                "dev-proj-470211", local_root=self.local_root,
            )
        expected = os.path.join(self.local_root, "dev-proj-470211")
        self.assertEqual(result, expected)
        self.assertTrue(os.path.isdir(result),
                        "hydrate must create the local dir")

    def test_hydrate_lists_blobs_under_correct_prefix(self):
        """Pin the prefix the SDK is asked to list under -- this is
        the contract that determines what the engine sees."""
        with patch.dict(os.environ, {"MTAGENT_STATE_BUCKET": "test-bucket"}):
            storage.hydrate_workdir(
                "dev-proj-470211", local_root=self.local_root,
            )
        # client.bucket(...) called with bucket name
        self.mock_client.bucket.assert_called_with("test-bucket")
        # client.list_blobs(bucket, prefix=...) called with the canonical prefix
        _, kwargs = self.mock_client.list_blobs.call_args
        self.assertEqual(
            kwargs.get("prefix"),
            "tenants/default/projects/dev-proj-470211/",
        )

    def test_hydrate_uses_tenant_id_when_provided(self):
        with patch.dict(os.environ, {"MTAGENT_STATE_BUCKET": "b"}):
            storage.hydrate_workdir(
                "dev-proj-470211",
                tenant_id="acme-corp",
                local_root=self.local_root,
            )
        _, kwargs = self.mock_client.list_blobs.call_args
        self.assertEqual(
            kwargs.get("prefix"),
            "tenants/acme-corp/projects/dev-proj-470211/",
        )

    def test_hydrate_downloads_each_blob_to_relative_path(self):
        """Each blob under the prefix should land under local_path
        at the same relative path. Pinned because the prefix-strip
        logic is easy to miscompute."""
        prefix = "tenants/default/projects/dev-proj-470211/"
        blobs = [
            self._make_blob(prefix + "main.tf", b"resource hello {}"),
            self._make_blob(prefix + "subdir/nested.tf", b"locals {}"),
        ]
        self.mock_client.list_blobs.return_value = iter(blobs)
        with patch.dict(os.environ, {"MTAGENT_STATE_BUCKET": "test-bucket"}):
            local = storage.hydrate_workdir(
                "dev-proj-470211", local_root=self.local_root,
            )
        # main.tf at top level
        self.assertTrue(os.path.isfile(os.path.join(local, "main.tf")))
        # nested file under subdir/ (parent dirs auto-created)
        self.assertTrue(os.path.isfile(
            os.path.join(local, "subdir", "nested.tf"),
        ))
        # File contents preserved
        with open(os.path.join(local, "main.tf"), "rb") as f:
            self.assertEqual(f.read(), b"resource hello {}")

    def test_hydrate_skips_zero_byte_directory_placeholders(self):
        """Some GCS layouts have empty 'directory' placeholders ending
        in '/'. They aren't real files; skip them so we don't try to
        download_to_filename a path with a trailing slash."""
        prefix = "tenants/default/projects/dev-proj-470211/"
        blobs = [
            # The placeholder
            self._make_blob(prefix, b""),
            self._make_blob(prefix + "subdir/", b""),
            # And one real file
            self._make_blob(prefix + "main.tf", b"x"),
        ]
        self.mock_client.list_blobs.return_value = iter(blobs)
        with patch.dict(os.environ, {"MTAGENT_STATE_BUCKET": "b"}):
            local = storage.hydrate_workdir(
                "dev-proj-470211", local_root=self.local_root,
            )
        # Only main.tf should exist locally
        self.assertTrue(os.path.isfile(os.path.join(local, "main.tf")))
        # Placeholder blobs should NOT have been download_to_filenamed
        blobs[0].download_to_filename.assert_not_called()
        blobs[1].download_to_filename.assert_not_called()

    def test_hydrate_validates_project_id(self):
        with self.assertRaises(ValueError):
            storage.hydrate_workdir("BAD-PROJECT", local_root=self.local_root)
        # SDK must NOT have been touched
        self.mock_client.list_blobs.assert_not_called()

    def test_hydrate_propagates_genuine_failure(self):
        """Auth / network / Forbidden propagate so the caller can
        decide (Streamlit page would render via render_error)."""
        self.mock_client.list_blobs.side_effect = gcs_exceptions.Forbidden(
            "403 Forbidden: The caller does not have permission",
        )
        with self.assertRaises(gcs_exceptions.Forbidden):
            storage.hydrate_workdir(
                "dev-proj-470211", local_root=self.local_root,
            )

    def test_hydrate_tolerates_notfound_exception(self):
        """Defensive: some SDK versions may raise NotFound on a
        missing prefix instead of returning an empty iterator. Both
        behaviours land at the SAME 'first run for this project'
        outcome -- empty workdir, no error."""
        self.mock_client.list_blobs.side_effect = gcs_exceptions.NotFound(
            "not found",
        )
        result = storage.hydrate_workdir(
            "dev-proj-470211", local_root=self.local_root,
        )
        self.assertEqual(
            result, os.path.join(self.local_root, "dev-proj-470211"),
        )

    def test_hydrate_tolerates_empty_iterator_first_run(self):
        """Modern SDK returns an empty iterator (NOT NotFound) when
        the prefix has no objects. This is the COMMON first-run
        path -- no exception, just zero downloads. Pinned because
        the PUI-1 smoke (2026-04-28) hit this path."""
        self.mock_client.list_blobs.return_value = iter([])
        result = storage.hydrate_workdir(
            "dev-proj-470211", local_root=self.local_root,
        )
        self.assertEqual(
            result, os.path.join(self.local_root, "dev-proj-470211"),
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
    """Pin persist's SDK invocation pattern + exclude application."""

    def setUp(self):
        self._patcher = patch.object(storage, "_get_gcs_client")
        self.mock_get_client = self._patcher.start()
        self.mock_client = MagicMock(name="gcs_client")
        self.mock_get_client.return_value = self.mock_client
        # Default: bucket() returns a mock, list_blobs returns empty
        # (no remote files to delete). Tests override as needed.
        self.mock_bucket = MagicMock(name="bucket")
        self.mock_client.bucket.return_value = self.mock_bucket
        self.mock_client.list_blobs.return_value = iter([])

        self._tmpdir = tempfile.TemporaryDirectory()
        self.local_path = os.path.join(self._tmpdir.name, "dev-proj-470211")
        os.makedirs(self.local_path, exist_ok=True)

    def tearDown(self):
        self._patcher.stop()
        self._tmpdir.cleanup()

    def _write(self, relpath: str, content: bytes = b"x") -> None:
        """Helper: write a fake file under self.local_path."""
        abs_path = os.path.join(self.local_path, relpath)
        os.makedirs(os.path.dirname(abs_path), exist_ok=True)
        with open(abs_path, "wb") as f:
            f.write(content)

    def test_persist_uploads_each_local_file_with_correct_blob_name(self):
        """Local files end up at prefix + relpath -- mirrors hydrate's
        layout so a hydrate→persist roundtrip is identity."""
        self._write("main.tf")
        self._write("subdir/nested.tf")
        with patch.dict(os.environ, {"MTAGENT_STATE_BUCKET": "test-bucket"}):
            storage.persist_workdir(self.local_path, "dev-proj-470211")
        # Each local file -> one bucket.blob() + upload_from_filename call.
        blob_names = [
            call.args[0] for call in self.mock_bucket.blob.call_args_list
        ]
        self.assertIn(
            "tenants/default/projects/dev-proj-470211/main.tf", blob_names,
        )
        self.assertIn(
            "tenants/default/projects/dev-proj-470211/subdir/nested.tf",
            blob_names,
        )

    def test_persist_skips_excluded_patterns(self):
        """_PERSIST_EXCLUDES files must NOT be uploaded. Pinned because
        a stray local terraform.tfstate getting persisted would clobber
        the GCS-backend state."""
        self._write("main.tf")
        self._write("terraform.tfstate")  # explicitly excluded
        self._write("backup.backup")  # *.backup excluded
        self._write("_diagnostics/blueprint.yaml")  # _diagnostics/** excluded
        with patch.dict(os.environ, {"MTAGENT_STATE_BUCKET": "b"}):
            storage.persist_workdir(self.local_path, "dev-proj-470211")
        blob_names = [
            call.args[0] for call in self.mock_bucket.blob.call_args_list
        ]
        # main.tf gets uploaded
        self.assertTrue(any("main.tf" in n for n in blob_names))
        # excluded files do NOT get uploaded
        for excluded in ("terraform.tfstate", ".backup",
                         "_diagnostics"):
            self.assertFalse(
                any(excluded in n for n in blob_names),
                f"excluded pattern should not appear in uploads: "
                f"{excluded} in {blob_names}",
            )

    def test_persist_deletes_remote_blobs_no_longer_in_local(self):
        """rsync --delete-unmatched-destination-objects equivalent:
        if a blob exists remotely but the matching local file is gone,
        delete it from GCS. Mirrors what the prior gcloud rsync did."""
        prefix = "tenants/default/projects/dev-proj-470211/"
        # Local has main.tf only
        self._write("main.tf")
        # Remote has main.tf AND old.tf (which was deleted locally)
        local_blob = MagicMock(name="local")
        local_blob.name = prefix + "main.tf"
        deleted_blob = MagicMock(name="should_delete")
        deleted_blob.name = prefix + "old.tf"
        self.mock_client.list_blobs.return_value = iter(
            [local_blob, deleted_blob],
        )
        with patch.dict(os.environ, {"MTAGENT_STATE_BUCKET": "b"}):
            storage.persist_workdir(self.local_path, "dev-proj-470211")
        # old.tf MUST be deleted; main.tf must NOT.
        deleted_blob.delete.assert_called_once()
        local_blob.delete.assert_not_called()

    def test_persist_uses_tenant_id_when_provided(self):
        self._write("main.tf")
        with patch.dict(os.environ, {"MTAGENT_STATE_BUCKET": "test-bucket"}):
            storage.persist_workdir(
                self.local_path, "dev-proj-470211", tenant_id="acme",
            )
        blob_names = [
            call.args[0] for call in self.mock_bucket.blob.call_args_list
        ]
        self.assertTrue(
            any("tenants/acme/projects/dev-proj-470211/" in n
                for n in blob_names),
            f"tenant prefix should appear in blob names: {blob_names}",
        )

    def test_persist_validates_project_id(self):
        with self.assertRaises(ValueError):
            storage.persist_workdir(self.local_path, "BAD-PROJECT")
        # SDK must NOT have been touched
        self.mock_client.list_blobs.assert_not_called()
        self.mock_bucket.blob.assert_not_called()

    def test_persist_raises_when_local_path_missing(self):
        bogus = os.path.join(self._tmpdir.name, "nonexistent")
        with self.assertRaises(FileNotFoundError):
            storage.persist_workdir(bogus, "dev-proj-470211")
        self.mock_client.list_blobs.assert_not_called()

    def test_persist_propagates_sdk_failure(self):
        """Auth / network / permission errors propagate up so the
        caller (Streamlit page) can render the failure."""
        self._write("main.tf")
        self.mock_bucket.blob.return_value.upload_from_filename.side_effect = (
            gcs_exceptions.Forbidden("403 permission denied")
        )
        with self.assertRaises(gcs_exceptions.Forbidden):
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
