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

from importer.gcp_client import (
    _is_zonal_location,
    _resolve_location_flag,
    extract_path_segment,
    friendly_name_from_display,
)


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

    def test_location_flag_only_used_for_generic_locations(self):
        """P2-3: KMS-style configs declare `location_flag` only and the
        location can be a region OR multi-region OR 'global' -- none of
        which fit the zone/region picker. The picker emits the configured
        flag with the location verbatim."""
        info = {"location_flag": "--location"}
        self.assertEqual(
            _resolve_location_flag(info, {"location": "us-east1"}),
            ["--location", "us-east1"],
        )
        # Multi-region
        self.assertEqual(
            _resolve_location_flag(info, {"location": "us"}),
            ["--location", "us"],
        )
        # 'global' tier
        self.assertEqual(
            _resolve_location_flag(info, {"location": "global"}),
            ["--location", "global"],
        )

    def test_location_flag_no_op_without_location(self):
        info = {"location_flag": "--location"}
        self.assertEqual(_resolve_location_flag(info, {}), [])
        self.assertEqual(_resolve_location_flag(info, {"location": None}), [])


class ExtractPathSegmentTests(unittest.TestCase):
    """Pin the asset-URN path segment extractor (P2-3).

    The helper drives parent-identifier discovery for nested resources
    (cluster name for node_pool, keyring name for crypto_key, etc.).
    Reliability matters because a wrong extraction silently produces
    an unresolvable describe-call argument that fails late.
    """

    def test_extracts_cluster_segment_from_node_pool_path(self):
        """Exercises the C5 case via the new generalised helper."""
        path = "//container.googleapis.com/projects/p1/zones/us-central1-a/clusters/my-cluster/nodePools/default-pool"
        self.assertEqual(extract_path_segment(path, "clusters"), "my-cluster")

    def test_extracts_keyring_segment_from_crypto_key_path(self):
        """Exercises the new P2-3 case for KMS crypto keys."""
        path = "//cloudkms.googleapis.com/projects/p1/locations/us-east1/keyRings/my-keyring/cryptoKeys/my-key"
        self.assertEqual(extract_path_segment(path, "keyRings"), "my-keyring")

    def test_extracts_for_regional_path(self):
        """Regional paths use `/locations/<region>/` instead of `/zones/<zone>/`."""
        path = "//container.googleapis.com/projects/p1/locations/us-central1/clusters/regional-cluster/nodePools/default"
        self.assertEqual(extract_path_segment(path, "clusters"), "regional-cluster")

    def test_returns_none_when_segment_absent(self):
        """Non-nested resource path: no parent segment -> None."""
        path = "//compute.googleapis.com/projects/p1/zones/us-central1-a/instances/my-vm"
        self.assertIsNone(extract_path_segment(path, "clusters"))
        self.assertIsNone(extract_path_segment(path, "keyRings"))

    def test_returns_none_when_segment_at_path_end(self):
        """Defensive: malformed path with the segment as the last component
        (no value following) returns None rather than IndexError."""
        path = "//container.googleapis.com/projects/p1/clusters"
        self.assertIsNone(extract_path_segment(path, "clusters"))

    def test_returns_none_for_empty_inputs(self):
        self.assertIsNone(extract_path_segment("", "clusters"))
        self.assertIsNone(extract_path_segment(None, "clusters"))  # type: ignore[arg-type]
        self.assertIsNone(extract_path_segment("//x/y/z", ""))

    def test_segment_match_is_case_sensitive(self):
        """`keyRings` (camelCase from API) != `keyrings` (lowercase). Per
        GCP's URN convention the segment is always camelCase. We don't
        case-fold so a typo in config produces a clean None, not a silent
        match against the wrong segment."""
        path = "//cloudkms.googleapis.com/projects/p1/locations/us/keyRings/k/cryptoKeys/x"
        self.assertEqual(extract_path_segment(path, "keyRings"), "k")
        self.assertIsNone(extract_path_segment(path, "keyrings"))


class FriendlyNameFromDisplayTests(unittest.TestCase):
    """Pin the URN-to-friendly-name normaliser added in P2-6 (CC-8).

    Three distinct asset shapes exercised, plus the no-op cases.
    Every one corresponds to a real failure observed in the Phase 2
    SMOKE against dev-proj-470211: keyring + topic + subscription
    HCL gen failed with `missing_resource_line` because the URN
    flowed through as an HCL resource label.
    """

    def test_kms_keyring_urn_to_short_name(self):
        """The canonical Phase 2 SMOKE failure case: KMS keyring."""
        urn = "projects/dev-proj-470211/locations/us-central1/keyRings/poc-keyring"
        self.assertEqual(friendly_name_from_display(urn), "poc-keyring")

    def test_kms_crypto_key_urn_to_short_name(self):
        """Nested URN: crypto_key under a key ring. Last segment wins."""
        urn = "projects/p/locations/l/keyRings/k/cryptoKeys/poc-key"
        self.assertEqual(friendly_name_from_display(urn), "poc-key")

    def test_pubsub_topic_urn_to_short_name(self):
        urn = "projects/dev-proj-470211/topics/poc-topic"
        self.assertEqual(friendly_name_from_display(urn), "poc-topic")

    def test_pubsub_subscription_urn_to_short_name(self):
        urn = "projects/dev-proj-470211/subscriptions/poc-subscription"
        self.assertEqual(friendly_name_from_display(urn), "poc-subscription")

    def test_short_name_returned_unchanged(self):
        """Most asset types (compute_instance, storage_bucket, etc.)
        have a short displayName like `poc-vm` -- the helper must be
        a no-op for those, otherwise we'd silently change correct
        names."""
        for name in ("poc-vm", "poc-disk", "poc-smoke-bucket-...", "POC Smoke SA"):
            with self.subTest(name=name):
                self.assertEqual(friendly_name_from_display(name), name)

    def test_falsy_inputs_returned_unchanged(self):
        """None / empty string -> caller falls back to `name.split('/')[-1]`
        for the asset name; helper must not transform falsy inputs."""
        self.assertIsNone(friendly_name_from_display(None))
        self.assertEqual(friendly_name_from_display(""), "")

    def test_trailing_slash_yields_empty_segment(self):
        """Defensive: malformed display ending in `/` returns the empty
        last segment -- caller's `or selected_asset['name'].split('/')[-1]`
        fallback then kicks in. Helper itself stays predictable rather
        than introducing None-vs-empty-string ambiguity."""
        self.assertEqual(friendly_name_from_display("projects/p/topics/"), "")


if __name__ == "__main__":
    unittest.main()
