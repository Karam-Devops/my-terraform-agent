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
        """Types without a per-service SDK handler fall back to
        asset_v1 path in gcp_client. None signals 'no handler'.
        Uses a fake tf_type that intentionally doesn't exist
        (PERF-T0b v2 covers all real ones)."""
        self.assertIsNone(
            router.get_handler("google_fake_unregistered_type"),
        )

    def test_describe_returns_none_for_unregistered(self):
        result = router.describe(
            "google_fake_unregistered_type", "dev-proj-470211", "x",
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


class FullDispatchCoverageTests(unittest.TestCase):
    """PERF-T0b v2: every tf_type in ASSET_TO_TERRAFORM_MAP must have
    a per-service describe handler.

    Without this pin, future contributors adding a new asset_type
    would silently fall back to the asset_v1 sparse path -- resulting
    in incomplete HCL generation. The test enforces 1:1 coverage so
    the regression we hit during PUI-1B SMOKE 2026-04-28 can't recur
    silently.
    """

    def test_every_supported_tf_type_has_a_handler(self):
        from importer import config
        all_tf_types = set(config.ASSET_TO_TERRAFORM_MAP.values())
        handler_types = set(router._HANDLERS.keys())
        missing = all_tf_types - handler_types
        self.assertEqual(
            missing, set(),
            f"Per-service describe handler missing for "
            f"{len(missing)} type(s): {sorted(missing)}. "
            f"Add a handler in importer/_describe_router.py and "
            f"register it in _HANDLERS dict.",
        )

    def test_handler_dict_has_no_orphans(self):
        """Reverse check: every handler must map to a real tf_type
        the importer actually supports. Catches typos in handler
        registration."""
        from importer import config
        all_tf_types = set(config.ASSET_TO_TERRAFORM_MAP.values())
        handler_types = set(router._HANDLERS.keys())
        orphans = handler_types - all_tf_types
        self.assertEqual(
            orphans, set(),
            f"Handler registered for unknown tf_type(s): {sorted(orphans)}. "
            f"Either remove the handler OR add the tf_type to "
            f"ASSET_TO_TERRAFORM_MAP.",
        )


class ComputeFamilyHandlerTests(unittest.TestCase):
    """Spot-check a few of the v2 compute handlers to confirm the
    pattern. Full per-handler tests aren't necessary -- they all
    follow the same 'lazy client + get() + proto-to-dict' shape, and
    the FullDispatchCoverageTests above pin registration."""

    def test_disk_requires_zone(self):
        with patch.object(router, "_get_compute_disks_client") as mc:
            result = router._describe_compute_disk("p", "disk-a")
            self.assertIsNone(result)
            mc.return_value.get.assert_not_called()

    def test_firewall_global_no_extras_required(self):
        """Firewalls are global (no zone/region required)."""
        mock_client = MagicMock()
        mock_fw = MagicMock()
        mock_fw._pb = MagicMock()
        mock_client.get.return_value = mock_fw
        with patch.object(router, "_get_compute_firewalls_client",
                          return_value=mock_client), \
             patch.object(router, "MessageToDict",
                          return_value={"name": "fw-a"}):
            result = router._describe_compute_firewall("p", "fw-a")
        self.assertIsNotNone(result)
        # Confirm SDK called with project + firewall (no zone/region kwarg)
        call_kwargs = mock_client.get.call_args.kwargs
        self.assertEqual(call_kwargs["project"], "p")
        self.assertEqual(call_kwargs["firewall"], "fw-a")
        self.assertNotIn("zone", call_kwargs)
        self.assertNotIn("region", call_kwargs)


class NestedResourceHandlerTests(unittest.TestCase):
    """Pin the parent-identifier requirement for nested resources
    (node_pool requires cluster, crypto_key requires keyring)."""

    def test_node_pool_requires_cluster_and_location(self):
        with patch.object(router, "_get_container_clusters_client") as mc:
            # No cluster -> None
            self.assertIsNone(router._describe_container_node_pool(
                "p", "pool-a", location="us-central1",
            ))
            # No location -> None
            self.assertIsNone(router._describe_container_node_pool(
                "p", "pool-a", cluster="cluster-x",
            ))
            mc.return_value.get_node_pool.assert_not_called()

    def test_crypto_key_requires_keyring_and_location(self):
        with patch.object(router, "_get_kms_client") as mc:
            self.assertIsNone(router._describe_kms_crypto_key(
                "p", "key-a", location="us-central1",
            ))
            self.assertIsNone(router._describe_kms_crypto_key(
                "p", "key-a", keyring="kr-x",
            ))
            mc.return_value.get_crypto_key.assert_not_called()


class PubSubHandlerTests(unittest.TestCase):
    """Pub/Sub handlers use a request= dict (different SDK shape from
    compute's positional kwargs); pin the path construction."""

    def test_topic_path_construction(self):
        mock_client = MagicMock()
        mock_topic = MagicMock()
        mock_topic._pb = MagicMock()
        mock_client.get_topic.return_value = mock_topic
        with patch.object(router, "_get_pubsub_publisher_client",
                          return_value=mock_client), \
             patch.object(router, "MessageToDict",
                          return_value={"name": "topic-a"}):
            router._describe_pubsub_topic("dev-proj-470211", "topic-a")
        request = mock_client.get_topic.call_args.kwargs["request"]
        self.assertEqual(
            request["topic"], "projects/dev-proj-470211/topics/topic-a",
        )

    def test_subscription_path_construction(self):
        mock_client = MagicMock()
        mock_sub = MagicMock()
        mock_sub._pb = MagicMock()
        mock_client.get_subscription.return_value = mock_sub
        with patch.object(router, "_get_pubsub_subscriber_client",
                          return_value=mock_client), \
             patch.object(router, "MessageToDict",
                          return_value={"name": "sub-a"}):
            router._describe_pubsub_subscription(
                "dev-proj-470211", "sub-a",
            )
        request = mock_client.get_subscription.call_args.kwargs["request"]
        self.assertEqual(
            request["subscription"],
            "projects/dev-proj-470211/subscriptions/sub-a",
        )


if __name__ == "__main__":
    unittest.main()
