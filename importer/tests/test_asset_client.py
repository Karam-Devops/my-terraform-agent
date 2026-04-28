# importer/tests/test_asset_client.py
"""Unit tests for importer._asset_client (PERF-T0).

Pin the SDK seam that replaced the legacy subprocess gcloud calls.
The conftest stubs google.cloud.asset_v1; tests patch
``_get_asset_client`` to inject MagicMock clients.

Coverage:
  * list_resources_of_type returns the legacy dict shape
  * list_resources_of_type tolerates NotFound (project missing /
    cloudasset API not enabled) by returning empty
  * list_resources_of_type re-raises PermissionDenied (real failure
    that the operator must fix via IAM grant)
  * get_resource_state matches by full URN OR short name OR displayName
  * get_resource_state returns None when no candidate matches
  * get_resource_state_as_json returns json string OR None
"""

from __future__ import annotations

import json
import unittest
from unittest.mock import patch, MagicMock

from importer import _asset_client
from google.api_core import exceptions as gcs_exceptions


def _make_asset(name: str, asset_type: str, data: dict | None = None):
    """Build a MagicMock Asset that looks like google.cloud.asset_v1.Asset.

    Replicates just enough of the real Asset shape that
    _asset_to_legacy_dict can transform it. The conftest stubs
    MessageToDict to return the data dict as-is, so we set
    asset.resource.data = the dict directly.
    """
    asset = MagicMock(name=f"asset:{name}")
    asset.name = name
    asset.asset_type = asset_type
    if data is not None:
        asset.resource = MagicMock()
        asset.resource.data = data  # MessageToDict stub returns this verbatim
    else:
        asset.resource = None
    return asset


class ListResourcesOfTypeTests(unittest.TestCase):
    """Pin the SDK invocation pattern + return shape."""

    def setUp(self):
        self._patcher = patch.object(_asset_client, "_get_asset_client")
        self.mock_get = self._patcher.start()
        self.mock_client = MagicMock(name="asset_client")
        self.mock_get.return_value = self.mock_client

    def tearDown(self):
        self._patcher.stop()

    def test_returns_legacy_dict_shape(self):
        """The output must match the dict shape downstream consumers
        (gcp_client._map_asset_to_terraform, inventory._to_cloud_resource)
        already expect: at minimum `name`, `assetType`, `displayName`."""
        assets = [
            _make_asset(
                "//compute.googleapis.com/projects/p/zones/us-central1-a/instances/vm-a",
                "compute.googleapis.com/Instance",
                data={"name": "projects/p/zones/us-central1-a/instances/vm-a",
                      "zone": "https://www.googleapis.com/compute/v1/projects/p/zones/us-central1-a"},
            ),
        ]
        self.mock_client.list_assets.return_value = iter(assets)
        result = _asset_client.list_resources_of_type(
            "dev-proj-470211", "compute.googleapis.com/Instance",
        )
        self.assertEqual(len(result), 1)
        self.assertIn("name", result[0])
        self.assertIn("assetType", result[0])
        self.assertEqual(result[0]["assetType"],
                         "compute.googleapis.com/Instance")

    def test_uses_correct_parent_and_asset_type(self):
        """SDK is called with `projects/<id>` parent + the asset_types
        list. Pinned because typos / wrong scope shape would silently
        return empty."""
        self.mock_client.list_assets.return_value = iter([])
        _asset_client.list_resources_of_type(
            "dev-proj-470211", "storage.googleapis.com/Bucket",
        )
        self.mock_client.list_assets.assert_called_once()
        request = self.mock_client.list_assets.call_args.kwargs["request"]
        self.assertEqual(request["parent"], "projects/dev-proj-470211")
        self.assertEqual(
            request["asset_types"], ["storage.googleapis.com/Bucket"],
        )

    def test_tolerates_notfound_returns_empty(self):
        """NotFound (project missing OR cloudasset API not enabled) is
        a normal-ish first-encounter case -- log + empty list, NOT a
        crash. The inventory layer counts it as one bad asset type
        but the workflow continues."""
        self.mock_client.list_assets.side_effect = gcs_exceptions.NotFound(
            "project not found",
        )
        result = _asset_client.list_resources_of_type(
            "dev-proj-470211", "compute.googleapis.com/Instance",
        )
        self.assertEqual(result, [])

    def test_propagates_permission_denied(self):
        """PermissionDenied is a configuration error the operator MUST
        fix (IAM grant). Re-raise so the failure surfaces in logs +
        the calling layer can wrap with a hint."""
        self.mock_client.list_assets.side_effect = gcs_exceptions.PermissionDenied(
            "403 caller lacks roles/cloudasset.viewer",
        )
        with self.assertRaises(gcs_exceptions.PermissionDenied):
            _asset_client.list_resources_of_type(
                "dev-proj-470211", "compute.googleapis.com/Instance",
            )


class GetResourceStateTests(unittest.TestCase):
    """Pin the per-resource state lookup contract."""

    def setUp(self):
        self._patcher = patch.object(_asset_client, "_get_asset_client")
        self.mock_get = self._patcher.start()
        self.mock_client = MagicMock(name="asset_client")
        self.mock_get.return_value = self.mock_client

    def tearDown(self):
        self._patcher.stop()

    def test_matches_by_short_name(self):
        """Most common case: caller passes a short name like 'vm-a'
        and we find the asset whose URN ends in '/vm-a'."""
        assets = [
            _make_asset(
                "//compute.googleapis.com/projects/p/zones/z/instances/vm-a",
                "compute.googleapis.com/Instance",
                data={"name": "projects/p/zones/z/instances/vm-a"},
            ),
        ]
        self.mock_client.list_assets.return_value = iter(assets)
        result = _asset_client.get_resource_state(
            "dev-proj-470211", "compute.googleapis.com/Instance", "vm-a",
        )
        self.assertIsNotNone(result)
        self.assertIn("name", result)

    def test_matches_by_full_urn(self):
        """Caller may also pass the full URN (e.g. detector forwarding
        what inventory returned)."""
        full_urn = ("//compute.googleapis.com/projects/p/zones/z/"
                    "instances/vm-a")
        assets = [
            _make_asset(full_urn, "compute.googleapis.com/Instance",
                        data={"name": "projects/p/zones/z/instances/vm-a"}),
        ]
        self.mock_client.list_assets.return_value = iter(assets)
        result = _asset_client.get_resource_state(
            "dev-proj-470211", "compute.googleapis.com/Instance", full_urn,
        )
        self.assertIsNotNone(result)

    def test_returns_none_when_no_match(self):
        """No asset with matching name -> None (caller treats as
        'describe failed' the same way the legacy gcloud subprocess
        returned empty stdout)."""
        assets = [
            _make_asset(
                "//compute.googleapis.com/projects/p/zones/z/instances/vm-a",
                "compute.googleapis.com/Instance",
                data={"name": "projects/p/zones/z/instances/vm-a"},
            ),
        ]
        self.mock_client.list_assets.return_value = iter(assets)
        result = _asset_client.get_resource_state(
            "dev-proj-470211", "compute.googleapis.com/Instance",
            "vm-does-not-exist",
        )
        self.assertIsNone(result)


class GetResourceStateAsJsonTests(unittest.TestCase):
    """Pin the JSON-serialised wrapper that gcp_client uses."""

    def setUp(self):
        self._patcher = patch.object(_asset_client, "_get_asset_client")
        self.mock_get = self._patcher.start()
        self.mock_client = MagicMock(name="asset_client")
        self.mock_get.return_value = self.mock_client

    def tearDown(self):
        self._patcher.stop()

    def test_returns_json_string_on_match(self):
        assets = [
            _make_asset(
                "//compute.googleapis.com/projects/p/zones/z/instances/vm-a",
                "compute.googleapis.com/Instance",
                data={"name": "projects/p/zones/z/instances/vm-a"},
            ),
        ]
        self.mock_client.list_assets.return_value = iter(assets)
        result = _asset_client.get_resource_state_as_json(
            "dev-proj-470211", "compute.googleapis.com/Instance", "vm-a",
        )
        self.assertIsNotNone(result)
        # Round-trips through json.loads (i.e. it's valid JSON)
        parsed = json.loads(result)
        self.assertIsInstance(parsed, dict)

    def test_returns_none_when_no_match(self):
        self.mock_client.list_assets.return_value = iter([])
        result = _asset_client.get_resource_state_as_json(
            "dev-proj-470211", "compute.googleapis.com/Instance", "missing",
        )
        self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()
