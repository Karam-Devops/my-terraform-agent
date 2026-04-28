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
        """Resource with none of the four resolution paths (zone /
        location / region / URN-extract) returns None. Caller uses
        None to decide "no location flag applies" — see
        _resolve_location_flag."""
        r = self._make(name="my-net", project="p")
        self.assertIsNone(r.location)

    # -- D-3 round 2: URN-extraction fallback ----------------------

    def test_extract_location_from_key_ring_urn(self):
        """google_kms_crypto_key has no top-level location attribute;
        it's encoded in the parent `key_ring` URN. Fallback must
        extract it. Pre-fix the detector emitted a gcloud command
        without --location and the describe call failed."""
        r = ManagedResource(
            tf_type="google_kms_crypto_key",
            hcl_name="poc_key",
            tf_address="google_kms_crypto_key.poc_key",
            attributes={
                "name": "poc-key",
                "key_ring": "projects/dev-proj-470211/locations/us-central1/"
                            "keyRings/poc-keyring",
                "id": "projects/dev-proj-470211/locations/us-central1/"
                      "keyRings/poc-keyring/cryptoKeys/poc-key",
            },
            in_scope=True,
        )
        self.assertEqual(r.location, "us-central1")

    def test_extract_location_from_id_when_key_ring_absent(self):
        """Fallback chain: if `key_ring` is missing/malformed, try
        `id`. Same URN pattern, more segments after the location."""
        r = self._make(
            id="projects/dev-proj-470211/locations/us-east1/"
               "keyRings/k/cryptoKeys/x",
            name="x",
        )
        self.assertEqual(r.location, "us-east1")

    def test_top_level_location_wins_over_urn_extract(self):
        """If a resource somehow has BOTH a top-level location AND
        a URN, top-level wins. Cheap happy-path stays cheap."""
        r = ManagedResource(
            tf_type="google_kms_crypto_key",
            hcl_name="x",
            tf_address="google_kms_crypto_key.x",
            attributes={
                "name": "x",
                "location": "explicit-region",
                "key_ring": "projects/p/locations/different-region/keyRings/k",
            },
            in_scope=True,
        )
        self.assertEqual(r.location, "explicit-region")

    def test_urn_extract_returns_none_for_non_urn_id(self):
        """Defensive: id that isn't a URN (e.g. bucket id == bucket
        name) returns None from URN extract; doesn't crash."""
        r = self._make(name="my-bucket", id="my-bucket")
        self.assertIsNone(r.location)

    def test_urn_extract_returns_none_for_empty_location_segment(self):
        """Defensive: URN with empty location segment doesn't return
        empty string. Fall through to None."""
        r = self._make(
            name="x",
            id="projects/p/locations//keyRings/k/cryptoKeys/x",
        )
        self.assertIsNone(r.location)


class ManagedResourceKeyringTests(unittest.TestCase):
    """D-3 round 2 (2026-04-28): pin the keyring-extraction property.

    Without this, detector's `_build_mapping` doesn't surface the
    parent keyring name, so gcloud's `--keyring` flag isn't set, and
    `gcloud kms keys describe` rejects the call as 'not properly
    specified'. Type-scoped to google_kms_crypto_key."""

    def _make(self, tf_type: str = "google_kms_crypto_key",
              **attrs) -> ManagedResource:
        return ManagedResource(
            tf_type=tf_type,
            hcl_name="x",
            tf_address=f"{tf_type}.x",
            attributes=attrs,
            in_scope=True,
        )

    def test_extracts_keyring_from_key_ring_urn(self):
        """Happy path: state's key_ring is the full URN; we want just
        the trailing keyring name."""
        r = self._make(
            name="poc-key",
            key_ring="projects/dev-proj-470211/locations/us-central1/"
                     "keyRings/poc-keyring",
        )
        self.assertEqual(r.keyring, "poc-keyring")

    def test_returns_none_for_non_crypto_key_types(self):
        """Type-scoped: even if a non-crypto_key resource has a
        `key_ring` attribute (unlikely but possible), don't return
        a keyring -- the parent flag wiring is crypto_key-specific."""
        r = self._make(
            tf_type="google_compute_instance",
            name="my-vm",
            key_ring="projects/p/locations/us-central1/keyRings/k",
        )
        self.assertIsNone(r.keyring)

    def test_returns_none_when_key_ring_missing(self):
        """Defensive: crypto_key without key_ring -> None.
        gcp_client's wiring (`if 'keyring' in mapping`) treats the
        absent key as 'no parent flag', skipping --keyring."""
        r = self._make(name="orphaned-key")
        self.assertIsNone(r.keyring)

    def test_returns_none_when_key_ring_malformed(self):
        """Malformed URN (missing 'keyRings' segment) -> None."""
        r = self._make(
            name="x",
            key_ring="projects/p/locations/us-central1",
        )
        self.assertIsNone(r.keyring)

    def test_returns_none_when_keyring_segment_empty(self):
        """URN ending in 'keyRings/' (empty segment) -> None."""
        r = self._make(
            name="x",
            key_ring="projects/p/locations/us-central1/keyRings/",
        )
        self.assertIsNone(r.keyring)

    def test_returns_none_when_key_ring_non_string(self):
        """Defensive against corrupted state (key_ring as None / int)."""
        r = self._make(name="x", key_ring=None)
        self.assertIsNone(r.keyring)
        r2 = self._make(name="x", key_ring=42)
        self.assertIsNone(r2.keyring)


if __name__ == "__main__":
    unittest.main()
