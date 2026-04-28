# detector/tests/test_state_reader_location.py
"""Pin the ManagedResource.location resolution contract.

D-1 fix (2026-04-28): without this property correctly handling the
`region` attribute, regional-only resource types (google_compute_subnetwork,
google_compute_address) had their describe calls fire without --region,
gcloud rejected them as "Underspecified resource", and the detector
silently treated them as "in sync" downstream. Tests below pin the
three known location-attribute conventions so a regression on any
single one would surface immediately.
"""

from __future__ import annotations

import unittest

from detector.state_reader import ManagedResource


class ManagedResourceLocationTests(unittest.TestCase):
    """Pin the location-attribute fallback chain.

    Different GCP resource types store their location under different
    state-attribute names. ManagedResource.location must resolve all
    three so downstream code (gcp_client.get_resource_details_json)
    can pick the right gcloud --zone / --region / --location flag.
    """

    def _make(self, **attrs) -> ManagedResource:
        """Tiny factory; only `attributes` matters for these tests."""
        return ManagedResource(
            tf_type="google_dummy",
            hcl_name="x",
            tf_address="google_dummy.x",
            attributes=attrs,
            in_scope=True,
        )

    # -- bucket 1: zonal-only types use `zone` -----------------------

    def test_zone_attribute_resolves(self):
        """google_compute_instance / google_compute_disk store
        location under `zone`."""
        r = self._make(zone="us-central1-a", project="p")
        self.assertEqual(r.location, "us-central1-a")

    # -- bucket 2: multi-region / location-agnostic use `location` ---

    def test_location_attribute_resolves(self):
        """google_storage_bucket / google_kms_key_ring /
        google_kms_crypto_key store location under `location`."""
        r = self._make(location="US", project="p")
        self.assertEqual(r.location, "US")

    # -- bucket 3: regional-only types use `region` (D-1 fix) --------

    def test_region_attribute_resolves(self):
        """google_compute_subnetwork / google_compute_address store
        location under `region`. Pre-D-1 this returned None, breaking
        the describe call's --region flag derivation."""
        r = self._make(region="us-central1", project="p")
        self.assertEqual(r.location, "us-central1")

    # -- precedence + fallback edge cases ---------------------------

    def test_zone_wins_over_location_when_both_present(self):
        """Defensive: if both `zone` and `location` are present, `zone`
        wins. Prevents accidental wrong-flag derivation in a future
        resource type that double-declares (no known case today, but
        the ordering is part of the contract)."""
        r = self._make(zone="us-central1-a", location="US", project="p")
        self.assertEqual(r.location, "us-central1-a")

    def test_location_wins_over_region_when_both_present(self):
        """Same defensive pattern: location wins over region (matches
        the chain order in the property). Pinning so the precedence
        is explicit."""
        r = self._make(location="US", region="us-central1", project="p")
        self.assertEqual(r.location, "US")

    def test_returns_none_when_no_attribute_matches(self):
        """Resource with none of the three attributes (e.g. global
        resources like google_compute_network, google_pubsub_topic)
        returns None. Caller uses None to decide "no location flag
        applies" — see _resolve_location_flag."""
        r = self._make(name="my-net", project="p")
        self.assertIsNone(r.location)


if __name__ == "__main__":
    unittest.main()
