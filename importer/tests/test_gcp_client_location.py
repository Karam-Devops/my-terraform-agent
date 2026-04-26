# importer/tests/test_gcp_client_location.py
"""C5.1 unit tests for the zone/region location-flag picker.

The bug surfaced in the Phase 1 SMOKE: regional GKE clusters
(location = "us-central1") had `gcloud container ... describe`
called with `--zone us-central1`, which gcloud rejects with
"Underspecified resource -- please specify --region".

These tests pin the picker's behaviour so the regression cannot
re-land. They are pure-function tests -- no gcloud, no mocks --
because `_resolve_location_flag` is intentionally side-effect free.
"""

from __future__ import annotations

import unittest

from importer.gcp_client import _is_zonal_location, _resolve_location_flag


class IsZonalLocationTests(unittest.TestCase):
    """Pin the zone-vs-region detector. Used by the dual-mode flag picker."""

    def test_zonal_locations_match(self):
        """Zones always end with `-<single letter>`."""
        for loc in (
            "us-central1-a",
            "us-central1-c",
            "us-east1-b",
            "europe-west1-d",
            "europe-north1-a",
            "asia-northeast1-c",
            "northamerica-northeast2-a",
            "me-central1-a",
        ):
            with self.subTest(loc=loc):
                self.assertTrue(_is_zonal_location(loc),
                                f"Expected zonal: {loc!r}")

    def test_regional_locations_do_not_match(self):
        """Regions never have a trailing zone-letter suffix."""
        for loc in (
            "us-central1",
            "us-east1",
            "europe-west1",
            "asia-northeast1",
            "me-central1",
            "northamerica-northeast2",
            "southamerica-east1",
        ):
            with self.subTest(loc=loc):
                self.assertFalse(_is_zonal_location(loc),
                                 f"Expected regional: {loc!r}")

    def test_falsy_inputs_return_false(self):
        """None / empty string / non-strings must not throw."""
        self.assertFalse(_is_zonal_location(""))
        self.assertFalse(_is_zonal_location(None))  # type: ignore[arg-type]


class ResolveLocationFlagTests(unittest.TestCase):
    """Pin the flag picker for all three resource shapes.

    Resource shapes we model:
      * Zonal-only      : declares zone_flag only   (compute_instance)
      * Regional-only   : declares region_flag only (compute_subnetwork)
      * Dual-mode       : declares both             (container_cluster,
                                                     container_node_pool)
      * Global / unknown: declares neither          (compute_network,
                                                     storage_bucket)
    """

    def test_zonal_only_resource_emits_zone_flag(self):
        info = {"zone_flag": "--zone"}
        mapping = {"location": "us-central1-a"}
        self.assertEqual(
            _resolve_location_flag(info, mapping),
            ["--zone", "us-central1-a"],
        )

    def test_regional_only_resource_emits_region_flag(self):
        info = {"region_flag": "--region"}
        mapping = {"location": "us-central1"}
        self.assertEqual(
            _resolve_location_flag(info, mapping),
            ["--region", "us-central1"],
        )

    def test_dual_mode_picks_zone_for_zonal_location(self):
        """Standard zonal GKE cluster: location = us-central1-a -> --zone."""
        info = {"zone_flag": "--zone", "region_flag": "--region"}
        mapping = {"location": "us-central1-a"}
        self.assertEqual(
            _resolve_location_flag(info, mapping),
            ["--zone", "us-central1-a"],
        )

    def test_dual_mode_picks_region_for_regional_location(self):
        """Autopilot regional cluster: location = us-central1 -> --region.

        This is the case that crashed the Phase 1 SMOKE; pinning it
        prevents the regression.
        """
        info = {"zone_flag": "--zone", "region_flag": "--region"}
        mapping = {"location": "us-central1"}
        self.assertEqual(
            _resolve_location_flag(info, mapping),
            ["--region", "us-central1"],
        )

    def test_no_flag_declared_returns_empty(self):
        """Global resources (network, bucket) declare neither flag."""
        self.assertEqual(_resolve_location_flag({}, {"location": "us-central1"}), [])

    def test_no_location_returns_empty(self):
        """If the mapping has no location, no flag is emitted regardless."""
        info = {"zone_flag": "--zone", "region_flag": "--region"}
        self.assertEqual(_resolve_location_flag(info, {}), [])
        self.assertEqual(_resolve_location_flag(info, {"location": None}), [])
        self.assertEqual(_resolve_location_flag(info, {"location": ""}), [])


if __name__ == "__main__":
    unittest.main()
