# importer/tests/test_inventory.py
"""P4-2 unit tests for importer.inventory.

Mocks gcp_client.discover_resources_of_type so the tests don't need a
live gcloud / GCP project. The pattern matches existing test files
(test_results.py, test_post_llm_overrides.py): direct
``from importer.X import Y`` works because the test runner sets
PYTHONPATH such that `importer` is a top-level package.

Coverage focus:
  * CloudResource shape: every field present, frozen, hashable
  * inventory() default mode: best-effort, swallows per-asset-type
    errors, returns sorted list
  * inventory(raise_on_error=True): strict mode raises InventoryError
    with project_id + failed_asset_types when any enumeration fails
  * Empty project returns empty list
  * URN-style displayName (KMS / Pub/Sub) collapses to short name
    via the shared friendly_name_from_display helper (CC-8 P2-6)
  * Asset_type → tf_type mapping is correct via config.ASSET_TO_TERRAFORM_MAP
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

from importer import config
from importer.inventory import (
    CloudResource,
    InventoryError,
    inventory,
)


class CloudResourceTests(unittest.TestCase):
    """The cloud-side counterpart to detector.state_reader.ManagedResource.
    P4-3 will set-diff these so they MUST be hashable."""

    def _sample(self, **overrides):
        base = dict(
            tf_type="google_compute_instance",
            asset_type="compute.googleapis.com/Instance",
            cloud_name="poc-vm",
            cloud_urn="//compute.googleapis.com/projects/p/zones/us-central1-a/instances/poc-vm",
            project_id="dev-proj-470211",
            location="us-central1-a",
        )
        base.update(overrides)
        return CloudResource(**base)

    def test_carries_all_required_fields(self):
        r = self._sample()
        self.assertEqual(r.tf_type, "google_compute_instance")
        self.assertEqual(r.cloud_name, "poc-vm")
        self.assertEqual(r.project_id, "dev-proj-470211")

    def test_is_hashable_for_set_diffing(self):
        # P4-3's DriftReport will compute set differences between
        # CloudResource (in cloud) and ManagedResource (in state) sets.
        # The hashability check is the contract.
        r1 = self._sample()
        r2 = self._sample()  # same identifying fields
        s = {r1, r2}
        self.assertEqual(len(s), 1)  # equal -> deduplicated

    def test_raw_asset_excluded_from_equality(self):
        # Two CloudResource instances are "the same resource" iff their
        # identifying fields match, regardless of raw_asset metadata
        # snapshot drift. This matters for diff stability across runs --
        # we don't want two enumerations of the same resource to compare
        # unequal because gcloud returned slightly different metadata.
        r1 = self._sample()
        r2 = self._sample()
        # Inject differing raw_asset contents.
        object.__setattr__(r1, "raw_asset", {"updateTime": "T1"})
        object.__setattr__(r2, "raw_asset", {"updateTime": "T2"})
        self.assertEqual(r1, r2)


class InventoryHappyPathTests(unittest.TestCase):

    def test_empty_project_returns_empty_list(self):
        # Every asset_type fetcher returns []. Result: empty list, no
        # error.
        with patch("importer.inventory.gcp_client.discover_resources_of_type",
                   return_value=[]):
            result = inventory("dev-proj-470211")
        self.assertEqual(result, [])

    def test_single_resource_mapped_correctly(self):
        # One instance discovered -- verify the asset_type → tf_type
        # mapping went through and the CloudResource carries the right
        # tf_type.
        def _fake_discover(project_id, asset_type):
            if asset_type == "compute.googleapis.com/Instance":
                return [{
                    "name": "//compute.googleapis.com/projects/p/zones/us-central1-a/instances/poc-vm",
                    "displayName": "poc-vm",
                    "location": "us-central1-a",
                }]
            return []
        with patch("importer.inventory.gcp_client.discover_resources_of_type",
                   side_effect=_fake_discover):
            result = inventory("dev-proj-470211")
        self.assertEqual(len(result), 1)
        r = result[0]
        self.assertEqual(r.tf_type, "google_compute_instance")
        self.assertEqual(r.asset_type, "compute.googleapis.com/Instance")
        self.assertEqual(r.cloud_name, "poc-vm")
        self.assertEqual(r.project_id, "dev-proj-470211")
        self.assertEqual(r.location, "us-central1-a")

    def test_urn_style_display_name_collapses_to_short_name(self):
        # CC-8 P2-6 fix: KMS keyrings + Pub/Sub topics return URN as
        # displayName. friendly_name_from_display should be applied so
        # cloud_name carries just the last path segment.
        def _fake_discover(project_id, asset_type):
            if asset_type == "cloudkms.googleapis.com/KeyRing":
                return [{
                    "name": "//cloudkms.googleapis.com/projects/p/locations/us-central1/keyRings/poc-keyring",
                    "displayName": "projects/p/locations/us-central1/keyRings/poc-keyring",
                }]
            return []
        with patch("importer.inventory.gcp_client.discover_resources_of_type",
                   side_effect=_fake_discover):
            result = inventory("dev-proj-470211")
        self.assertEqual(len(result), 1)
        # cloud_name should be the short name, NOT the URN.
        self.assertEqual(result[0].cloud_name, "poc-keyring")

    def test_results_sorted_deterministically(self):
        # P4-3 set-diff and golden tests need stable ordering across runs.
        # Sorted by (tf_type, cloud_name).
        def _fake_discover(project_id, asset_type):
            if asset_type == "compute.googleapis.com/Instance":
                return [
                    {"name": "n1", "displayName": "vm-z"},
                    {"name": "n2", "displayName": "vm-a"},
                ]
            if asset_type == "storage.googleapis.com/Bucket":
                return [{"name": "n3", "displayName": "bucket-m"}]
            return []
        with patch("importer.inventory.gcp_client.discover_resources_of_type",
                   side_effect=_fake_discover):
            result = inventory("dev-proj-470211")
        # tf_type order: google_compute_instance < google_storage_bucket
        # cloud_name order within compute: vm-a < vm-z
        labels = [(r.tf_type, r.cloud_name) for r in result]
        self.assertEqual(labels, [
            ("google_compute_instance", "vm-a"),
            ("google_compute_instance", "vm-z"),
            ("google_storage_bucket", "bucket-m"),
        ])

    def test_every_asset_type_in_config_is_attempted(self):
        # Defensive: if config.ASSET_TO_TERRAFORM_MAP grows (Phase 4
        # CG-2 will add types), inventory() must exercise the new ones
        # too. Spy on the mock to confirm every key was queried.
        called_for: list[str] = []
        def _record_calls(project_id, asset_type):
            called_for.append(asset_type)
            return []
        with patch("importer.inventory.gcp_client.discover_resources_of_type",
                   side_effect=_record_calls):
            inventory("dev-proj-470211")
        self.assertEqual(
            sorted(called_for),
            sorted(config.ASSET_TO_TERRAFORM_MAP.keys()),
            "inventory() must query every asset_type in the config map "
            "-- if you added a new type and this test fails, that's the "
            "missing wiring",
        )


class InventoryErrorPathTests(unittest.TestCase):

    def test_per_asset_type_error_swallowed_by_default(self):
        # Default mode (raise_on_error=False) preserves importer's
        # historical best-effort behavior: one failed asset_type
        # logs WARN + continues; other asset_types still enumerate.
        def _fake_discover(project_id, asset_type):
            if asset_type == "compute.googleapis.com/Instance":
                raise RuntimeError("simulated gcloud failure")
            if asset_type == "storage.googleapis.com/Bucket":
                return [{"name": "n1", "displayName": "good-bucket"}]
            return []
        with patch("importer.inventory.gcp_client.discover_resources_of_type",
                   side_effect=_fake_discover):
            result = inventory("dev-proj-470211")
        # Failure didn't kill the run; the bucket still came through.
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].cloud_name, "good-bucket")

    def test_raise_on_error_propagates_failure(self):
        # Strict mode for Detector use: any failure raises InventoryError
        # carrying the failed asset types so the caller can decide how
        # to surface partial-inventory to the operator.
        def _fake_discover(project_id, asset_type):
            if asset_type == "compute.googleapis.com/Instance":
                raise RuntimeError("simulated gcloud failure")
            return []
        with patch("importer.inventory.gcp_client.discover_resources_of_type",
                   side_effect=_fake_discover):
            with self.assertRaises(InventoryError) as ctx:
                inventory("dev-proj-470211", raise_on_error=True)
        self.assertEqual(ctx.exception.project_id, "dev-proj-470211")
        self.assertIn("compute.googleapis.com/Instance",
                      ctx.exception.failed_asset_types)

    def test_raise_on_error_succeeds_when_no_failures(self):
        # Strict mode is a no-op when nothing fails -- doesn't raise on
        # the empty project case.
        with patch("importer.inventory.gcp_client.discover_resources_of_type",
                   return_value=[]):
            # Should not raise.
            result = inventory("dev-proj-470211", raise_on_error=True)
        self.assertEqual(result, [])


if __name__ == "__main__":
    unittest.main()
