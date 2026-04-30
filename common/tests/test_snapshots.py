# common/tests/test_snapshots.py
"""Unit tests for common.snapshots (PSA-9).

Covers:
  * snapshots_enabled() env-var gate (default OFF, truthy aliases)
  * write_snapshot: gated correctly, validates inputs, writes both
    history + latest objects via gcloud, JSON-serializes payload
  * read_latest_snapshot: returns dict on success, None on missing
    (which is the documented "engine hasn't run yet" path)
  * Path-traversal guards on tenant_id + project_id
  * engine_name allowlist enforcement

All tests mock subprocess.run via the ``_run_gcloud`` seam --
no real GCS calls fire.
"""

from __future__ import annotations

import json
import os
import unittest
from unittest.mock import patch, MagicMock

from common import snapshots
# google.api_core.exceptions stubbed via common/tests/conftest.py.
from google.api_core import exceptions as gcs_exceptions


class SnapshotsEnabledTests(unittest.TestCase):
    """Pin the env-var gate. Default OFF preserves local-dev behaviour."""

    def test_unset_returns_false(self):
        env = {k: v for k, v in os.environ.items()
               if k != "MTAGENT_PERSIST_SNAPSHOTS"}
        with patch.dict(os.environ, env, clear=True):
            self.assertFalse(snapshots.snapshots_enabled())

    def test_truthy_aliases_work(self):
        for val in ("1", "true", "yes", "on", "TRUE", "On"):
            with self.subTest(val=val):
                with patch.dict(os.environ,
                                {"MTAGENT_PERSIST_SNAPSHOTS": val}):
                    self.assertTrue(snapshots.snapshots_enabled())

    def test_falsy_values_return_false(self):
        for val in ("0", "false", "no", "off", "", "FALSE"):
            with self.subTest(val=val):
                with patch.dict(os.environ,
                                {"MTAGENT_PERSIST_SNAPSHOTS": val}):
                    self.assertFalse(snapshots.snapshots_enabled())


class WriteSnapshotGatingTests(unittest.TestCase):
    """When env is OFF, write_snapshot must NOT touch the SDK."""

    def test_no_sdk_calls_when_disabled(self):
        env = {k: v for k, v in os.environ.items()
               if k != "MTAGENT_PERSIST_SNAPSHOTS"}
        with patch.dict(os.environ, env, clear=True):
            with patch.object(snapshots, "_get_gcs_client") as mock_get:
                result = snapshots.write_snapshot(
                    "importer", {"foo": "bar"}, "dev-proj-470211",
                )
        self.assertFalse(result, "should return False when disabled")
        mock_get.assert_not_called()

    def test_no_validation_when_disabled(self):
        """When env is off, even bogus inputs return cleanly without
        raising. Defensive against accidental local-dev calls."""
        env = {k: v for k, v in os.environ.items()
               if k != "MTAGENT_PERSIST_SNAPSHOTS"}
        with patch.dict(os.environ, env, clear=True):
            # Bad engine_name + bad project_id; both should NOT raise
            result = snapshots.write_snapshot(
                "bogus-engine", {"foo": 1}, "BAD-PROJECT",
            )
        self.assertFalse(result)


class WriteSnapshotEnabledPathTests(unittest.TestCase):
    """Pin the SDK upload pattern + payload when env is ON."""

    def setUp(self):
        self._env_patcher = patch.dict(os.environ, {
            "MTAGENT_PERSIST_SNAPSHOTS": "1",
            "MTAGENT_STATE_BUCKET": "test-bucket",
        })
        self._env_patcher.start()
        self._client_patcher = patch.object(snapshots, "_get_gcs_client")
        self.mock_get_client = self._client_patcher.start()
        self.mock_client = MagicMock(name="gcs_client")
        self.mock_get_client.return_value = self.mock_client
        # Track every (bucket_name, blob_name) seen so tests can
        # assert what got uploaded where.
        self.upload_log: list = []  # list of (bucket, blob_name, payload)
        self.mock_client.bucket.side_effect = (
            lambda name: self._make_bucket(name)
        )

    def tearDown(self):
        self._client_patcher.stop()
        self._env_patcher.stop()

    def _make_bucket(self, bucket_name: str):
        """Build a MagicMock bucket whose blob() returns a MagicMock
        blob recording its uploads into self.upload_log."""
        bucket = MagicMock(name=f"bucket:{bucket_name}")
        def _blob(blob_name: str):
            blob = MagicMock(name=f"blob:{blob_name}")
            def _upload(payload, content_type=None):
                self.upload_log.append((bucket_name, blob_name, payload))
            blob.upload_from_string.side_effect = _upload
            return blob
        bucket.blob.side_effect = _blob
        return bucket

    def test_writes_two_uploads_history_then_latest(self):
        """history first (immutable), latest second (overwrite).
        Order matters: a Dashboard read mid-write sees the previous
        latest until the new one lands."""
        snapshots.write_snapshot(
            "importer", {"imported": 13}, "dev-proj-470211",
        )
        self.assertEqual(len(self.upload_log), 2)
        # First upload = history
        bucket1, blob1, _ = self.upload_log[0]
        self.assertEqual(bucket1, "test-bucket")
        self.assertIn("snapshots/importer/history/", blob1)
        self.assertTrue(blob1.endswith(".json"))
        # Second upload = latest
        bucket2, blob2, _ = self.upload_log[1]
        self.assertEqual(bucket2, "test-bucket")
        self.assertTrue(
            blob2.endswith("snapshots/importer/latest.json"),
            f"latest blob path mismatch: {blob2}",
        )

    def test_payload_is_valid_json_with_result_dict(self):
        """Both uploads carry the JSON-serialized envelope wrapping
        the result dict (PUI-2pre gap #2 -- pre-PUI-2pre payload
        was the bare result; now it's
        {engine, written_at, tenant_id, project_id, data: result})."""
        result_dict = {
            "imported": 13, "needs_attention": 3,
            "skipped": 0, "failed": 0,
        }
        snapshots.write_snapshot(
            "importer", result_dict, "dev-proj-470211",
        )
        # Both upload payloads parse back to an envelope whose .data
        # equals the original result dict + envelope metadata fields.
        for _bucket, _blob, payload in self.upload_log:
            parsed = json.loads(payload)
            self.assertEqual(parsed["data"], result_dict)
            self.assertEqual(parsed["engine"], "importer")
            self.assertEqual(parsed["project_id"], "dev-proj-470211")
            self.assertEqual(parsed["tenant_id"], "default")
            # written_at is an ISO-8601 timestamp -- shape-test only
            # (exact value is mocked time-dependent).
            self.assertIsInstance(parsed["written_at"], str)
            self.assertTrue(parsed["written_at"].endswith("Z"))

    def test_destination_blob_path_uses_correct_tenant_and_engine(self):
        snapshots.write_snapshot(
            "translator", {"translated": 12}, "dev-proj-470211",
            tenant_id="acme-corp",
        )
        # Latest blob path includes tenant + project + engine
        _bucket, blob_path, _payload = self.upload_log[1]
        self.assertEqual(
            blob_path,
            "tenants/acme-corp/projects/dev-proj-470211/"
            "snapshots/translator/latest.json",
        )

    def test_validates_engine_name_when_enabled(self):
        with self.assertRaises(ValueError):
            snapshots.write_snapshot(
                "bogus-engine", {"x": 1}, "dev-proj-470211",
            )
        self.mock_get_client.assert_not_called()

    def test_validates_project_id_when_enabled(self):
        with self.assertRaises(ValueError):
            snapshots.write_snapshot(
                "importer", {"x": 1}, "BAD-PROJECT",
            )
        self.mock_get_client.assert_not_called()

    def test_all_4_engines_accepted(self):
        """importer / translator / detector / policy: all valid."""
        for engine in ("importer", "translator", "detector", "policy"):
            with self.subTest(engine=engine):
                self.upload_log.clear()
                result = snapshots.write_snapshot(
                    engine, {"x": 1}, "dev-proj-470211",
                )
                self.assertTrue(result)
                # Two uploads: history + latest
                self.assertEqual(len(self.upload_log), 2)


class ReadLatestSnapshotTests(unittest.TestCase):
    """Pin the read path: success returns dict, missing returns None."""

    def setUp(self):
        self._env_patcher = patch.dict(os.environ, {
            "MTAGENT_STATE_BUCKET": "test-bucket",
        })
        self._env_patcher.start()
        self._client_patcher = patch.object(snapshots, "_get_gcs_client")
        self.mock_get_client = self._client_patcher.start()
        self.mock_client = MagicMock(name="gcs_client")
        self.mock_get_client.return_value = self.mock_client
        # Track every blob path requested for assertions.
        self.requested_paths: list = []
        self.mock_client.bucket.side_effect = (
            lambda name: self._make_bucket(name)
        )

    def tearDown(self):
        self._client_patcher.stop()
        self._env_patcher.stop()

    def _make_bucket(self, bucket_name: str, fake_content=None,
                     raise_exc=None):
        """Build a MagicMock bucket whose blob.download_as_text returns
        fake_content (or raises raise_exc). Subclasses override the
        defaults via setattr after construction."""
        bucket = MagicMock(name=f"bucket:{bucket_name}")
        def _blob(blob_name: str):
            self.requested_paths.append(blob_name)
            blob = MagicMock(name=f"blob:{blob_name}")
            blob.download_as_text.side_effect = (
                self._download_behavior
            )
            return blob
        bucket.blob.side_effect = _blob
        return bucket

    # The download behavior is overridable per-test; default raises
    # NotFound so the "engine hasn't run yet" branch fires unless a
    # test sets a successful payload.
    def _download_behavior(self, *_args, **_kwargs):
        if hasattr(self, "_download_payload"):
            return self._download_payload
        raise gcs_exceptions.NotFound("not found")

    def test_returns_dict_on_successful_download(self):
        """blob.download_as_text returns valid JSON -> parsed dict."""
        self._download_payload = '{"imported": 13, "needs_attention": 3}'
        result = snapshots.read_latest_snapshot(
            "importer", "dev-proj-470211",
        )
        self.assertEqual(result, {"imported": 13, "needs_attention": 3})

    def test_returns_none_when_object_missing(self):
        """NotFound -> None. Documented contract: 'engine hasn't run yet'."""
        # Default _download_behavior raises NotFound; nothing to set.
        result = snapshots.read_latest_snapshot(
            "importer", "dev-proj-470211",
        )
        self.assertIsNone(result)

    def test_returns_none_when_json_malformed(self):
        """Downloaded text is not valid JSON -> None (defensive)."""
        self._download_payload = "this is not json {"
        result = snapshots.read_latest_snapshot(
            "importer", "dev-proj-470211",
        )
        self.assertIsNone(result)

    def test_returns_none_on_other_exception(self):
        """Network / Forbidden / other failures -> None (Dashboard
        renders empty-state gracefully). Logged at WARNING so
        operators can spot recurring failures."""
        # Override behavior to raise Forbidden on access
        def _raise_forbidden(*_a, **_kw):
            raise gcs_exceptions.Forbidden("403")
        self._download_behavior = _raise_forbidden  # type: ignore[assignment]
        result = snapshots.read_latest_snapshot(
            "importer", "dev-proj-470211",
        )
        self.assertIsNone(result)

    def test_validates_engine_name(self):
        """Read also validates engine name (catches typos at call site)."""
        with self.assertRaises(ValueError):
            snapshots.read_latest_snapshot(
                "bogus", "dev-proj-470211",
            )
        self.mock_get_client.assert_not_called()

    def test_uses_correct_blob_path(self):
        """Read path matches write path shape (writes + reads stay
        in sync as the prefix logic evolves)."""
        self._download_payload = '{}'
        snapshots.read_latest_snapshot(
            "detector", "dev-proj-470211", tenant_id="acme",
        )
        # The blob path requested for this read.
        self.assertIn(
            "tenants/acme/projects/dev-proj-470211/"
            "snapshots/detector/latest.json",
            self.requested_paths,
        )


class IdValidationTests(unittest.TestCase):
    """Pin the path-traversal guards on tenant_id + project_id."""

    def test_rejects_path_traversal_in_tenant_id(self):
        with self.assertRaises(ValueError):
            snapshots._validate_ids("../../etc", "dev-proj-470211")

    def test_rejects_path_traversal_in_project_id(self):
        with self.assertRaises(ValueError):
            snapshots._validate_ids("default", "../etc/passwd")

    def test_rejects_uppercase_project_id(self):
        with self.assertRaises(ValueError):
            snapshots._validate_ids("default", "DEV-PROJ-470211")

    def test_accepts_valid_ids(self):
        snapshots._validate_ids("default", "dev-proj-470211")
        snapshots._validate_ids("acme-corp_prod", "dev-proj-470211")


if __name__ == "__main__":
    unittest.main()
