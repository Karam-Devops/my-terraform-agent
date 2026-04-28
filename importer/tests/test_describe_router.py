# importer/tests/test_describe_router.py
"""Unit tests for importer._describe_router (PERF-T0b).

Pin the dispatch + per-handler contracts so:
  * Adding a new handler is one TODO entry to add a test row, not
    a debug session.
  * The asset_v1 fallback path stays wired (gcp_client uses it for
    types without a handler yet).
  * snake_case -> camelCase conversion preserves nested dict shapes
    (LLM was tested against gcloud's camelCase output).

Mocks the per-service SDK clients so no real GCP calls fire. Mock
SDKs return proto-plus-shaped objects with a ``_pb`` attribute and
``_properties`` dict to match the real SDK return shape.
"""

from __future__ import annotations

import unittest
from unittest.mock import patch, MagicMock

from importer import _describe_router as router


class CamelCaseConverterTests(unittest.TestCase):
    """Pin _to_camel and _camelize_keys for the snake_case -> camelCase
    conversion that matches gcloud's JSON output."""

    def test_snake_to_camel_single_word(self):
        self.assertEqual(router._to_camel("name"), "name")

    def test_snake_to_camel_multi_word(self):
        self.assertEqual(router._to_camel("storage_class"), "storageClass")
        self.assertEqual(
            router._to_camel("iam_configuration"), "iamConfiguration",
        )

    def test_camelize_dict(self):
        result = router._camelize_keys({
            "storage_class": "STANDARD",
            "iam_configuration": {"public_access_prevention": "enforced"},
        })
        self.assertEqual(result, {
            "storageClass": "STANDARD",
            "iamConfiguration": {"publicAccessPrevention": "enforced"},
        })

    def test_camelize_list_of_dicts(self):
        result = router._camelize_keys([
            {"network_interface": "x"},
            {"machine_type": "y"},
        ])
        self.assertEqual(result, [
            {"networkInterface": "x"},
            {"machineType": "y"},
        ])

    def test_camelize_passes_through_non_dict_values(self):
        self.assertEqual(router._camelize_keys("string"), "string")
        self.assertEqual(router._camelize_keys(42), 42)
        self.assertIsNone(router._camelize_keys(None))


class DispatchTests(unittest.TestCase):
    """Pin the get_handler + describe dispatch contract."""

    def test_handler_returns_for_registered_types(self):
        self.assertIsNotNone(
            router.get_handler("google_storage_bucket"),
        )
        self.assertIsNotNone(
            router.get_handler("google_compute_instance"),
        )
        self.assertIsNotNone(
            router.get_handler("google_container_cluster"),
        )

    def test_handler_returns_none_for_unregistered(self):
        """Types without a per-service SDK handler yet fall back to
        asset_v1 path in gcp_client. None signals 'no handler'."""
        self.assertIsNone(
            router.get_handler("google_kms_crypto_key"),
        )
        self.assertIsNone(
            router.get_handler("google_pubsub_topic"),
        )

    def test_describe_returns_none_for_unregistered(self):
        result = router.describe(
            "google_kms_crypto_key", "dev-proj-470211", "key-x",
        )
        self.assertIsNone(result)


class StorageBucketHandlerTests(unittest.TestCase):
    """Pin _describe_storage_bucket: returns the bucket._properties dict
    matching gcloud storage buckets describe output (camelCase)."""

    def setUp(self):
        self._patcher = patch.object(router, "_get_storage_client")
        self.mock_get = self._patcher.start()
        self.mock_client = MagicMock()
        self.mock_get.return_value = self.mock_client

    def tearDown(self):
        self._patcher.stop()

    def test_returns_bucket_properties_dict(self):
        """The handler reads bucket._properties (the raw REST response
        dict, already camelCase per the SDK's contract)."""
        mock_bucket = MagicMock()
        mock_bucket._properties = {
            "name": "poc-smoke-bucket-dev-proj-470211",
            "location": "US",
            "storageClass": "STANDARD",
            "iamConfiguration": {"publicAccessPrevention": "enforced"},
        }
        self.mock_client.get_bucket.return_value = mock_bucket
        result = router._describe_storage_bucket(
            "dev-proj-470211", "poc-smoke-bucket-dev-proj-470211",
        )
        # All fields preserved; matches gcloud output shape
        self.assertEqual(result["name"], "poc-smoke-bucket-dev-proj-470211")
        self.assertEqual(result["location"], "US")
        self.assertEqual(result["storageClass"], "STANDARD")
        self.assertIn("iamConfiguration", result)

    def test_returns_none_on_not_found(self):
        from google.api_core import exceptions as gcs_exceptions
        self.mock_client.get_bucket.side_effect = gcs_exceptions.NotFound("x")
        result = router._describe_storage_bucket(
            "dev-proj-470211", "missing-bucket",
        )
        self.assertIsNone(result)

    def test_includes_required_field_for_terraform(self):
        """The whole point of PERF-T0b: the bucket dict MUST include
        `location`, which is REQUIRED for google_storage_bucket HCL.
        Without this, the LLM generates incomplete HCL and terraform
        plan-verify fails (as observed in PUI-1B SMOKE 2026-04-28)."""
        mock_bucket = MagicMock()
        mock_bucket._properties = {
            "name": "x", "location": "US",
        }
        self.mock_client.get_bucket.return_value = mock_bucket
        result = router._describe_storage_bucket("p", "x")
        self.assertIn(
            "location", result,
            "location is required for google_storage_bucket HCL "
            "and must always be present in the describe output",
        )


class ComputeInstanceHandlerTests(unittest.TestCase):
    """Pin _describe_compute_instance contract."""

    def setUp(self):
        self._patcher = patch.object(router, "_get_compute_instances_client")
        self.mock_get = self._patcher.start()
        self.mock_client = MagicMock()
        self.mock_get.return_value = self.mock_client

    def tearDown(self):
        self._patcher.stop()

    def test_requires_zone_in_extras(self):
        """Compute instances are zonal; without zone the SDK call
        can't succeed. Logged + None returned."""
        result = router._describe_compute_instance(
            "dev-proj-470211", "vm-a",
        )
        self.assertIsNone(result)
        # SDK shouldn't have been called
        self.mock_client.get.assert_not_called()

    def test_passes_zone_to_sdk(self):
        """Zone in extras must propagate to the SDK call."""
        # Mock the instance returned by the SDK
        mock_instance = MagicMock()
        mock_instance._pb = MagicMock()
        self.mock_client.get.return_value = mock_instance
        # Patch MessageToDict to return a known dict (avoid proto plumbing)
        with patch.object(router, "MessageToDict",
                          return_value={"name": "vm-a"}):
            router._describe_compute_instance(
                "dev-proj-470211", "vm-a", zone="us-central1-a",
            )
        # Verify SDK called with project + zone + instance
        call_kwargs = self.mock_client.get.call_args.kwargs
        self.assertEqual(call_kwargs["project"], "dev-proj-470211")
        self.assertEqual(call_kwargs["zone"], "us-central1-a")
        self.assertEqual(call_kwargs["instance"], "vm-a")

    def test_accepts_location_alias_for_zone(self):
        """The mapping dict from inventory often uses `location` not
        `zone`; the handler should accept either."""
        mock_instance = MagicMock()
        mock_instance._pb = MagicMock()
        self.mock_client.get.return_value = mock_instance
        with patch.object(router, "MessageToDict",
                          return_value={"name": "vm-a"}):
            router._describe_compute_instance(
                "dev-proj-470211", "vm-a", location="us-central1-a",
            )
        call_kwargs = self.mock_client.get.call_args.kwargs
        self.assertEqual(call_kwargs["zone"], "us-central1-a")


class ContainerClusterHandlerTests(unittest.TestCase):
    """Pin _describe_container_cluster contract."""

    def setUp(self):
        self._patcher = patch.object(router, "_get_container_clusters_client")
        self.mock_get = self._patcher.start()
        self.mock_client = MagicMock()
        self.mock_get.return_value = self.mock_client

    def tearDown(self):
        self._patcher.stop()

    def test_requires_location(self):
        result = router._describe_container_cluster(
            "dev-proj-470211", "cluster-a",
        )
        self.assertIsNone(result)
        self.mock_client.get_cluster.assert_not_called()

    def test_constructs_canonical_cluster_path(self):
        """SDK requires `name=projects/<P>/locations/<L>/clusters/<C>`."""
        mock_cluster = MagicMock()
        mock_cluster._pb = MagicMock()
        self.mock_client.get_cluster.return_value = mock_cluster
        with patch.object(router, "MessageToDict",
                          return_value={"name": "cluster-a"}):
            router._describe_container_cluster(
                "dev-proj-470211", "cluster-a", location="us-central1",
            )
        call_kwargs = self.mock_client.get_cluster.call_args.kwargs
        self.assertEqual(
            call_kwargs["name"],
            "projects/dev-proj-470211/locations/us-central1/clusters/cluster-a",
        )


if __name__ == "__main__":
    unittest.main()
