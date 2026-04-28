# detector/tests/test_state_reader_resource_name.py
"""Pin the ManagedResource.resource_name resolution contract.

D-2 fix (2026-04-28): google_service_account stores `name` as the
canonical full path (`projects/<P>/serviceAccounts/<email>`), but
`gcloud iam service-accounts describe` expects just the email. The
generic `attributes["name"]` lookup that worked for other types broke
SA describes silently. Tests below pin both the common-case behavior
AND the SA exception so a future refactor can't quietly regress
either.
"""

from __future__ import annotations

import unittest

from detector.state_reader import ManagedResource


class ManagedResourceResourceNameTests(unittest.TestCase):
    """Pin the resource_name property's behavior across resource types."""

    def _make(self, tf_type: str, **attrs) -> ManagedResource:
        return ManagedResource(
            tf_type=tf_type,
            hcl_name="x",
            tf_address=f"{tf_type}.x",
            attributes=attrs,
            in_scope=True,
        )

    # -- Common case: most types use `name` as-is -------------------

    def test_compute_instance_uses_name_attribute(self):
        """google_compute_instance.name is the short, gcloud-friendly
        name. No special handling needed."""
        r = self._make("google_compute_instance", name="poc-vm",
                       project="p", zone="us-central1-a")
        self.assertEqual(r.resource_name, "poc-vm")

    def test_kms_key_ring_uses_name_attribute(self):
        """google_kms_key_ring.name is just the keyring name (e.g.
        'poc-keyring'), not the full path. Same as the common pattern."""
        r = self._make("google_kms_key_ring", name="poc-keyring",
                       project="p", location="us-central1")
        self.assertEqual(r.resource_name, "poc-keyring")

    def test_storage_bucket_uses_name_attribute(self):
        """google_storage_bucket.name is the bucket name."""
        r = self._make("google_storage_bucket", name="my-bucket-xyz",
                       project="p", location="US")
        self.assertEqual(r.resource_name, "my-bucket-xyz")

    # -- D-2 fix: google_service_account special case ---------------

    def test_service_account_prefers_email_over_name(self):
        """google_service_account.name is the full canonical path, but
        gcloud expects just the email. Verify `email` wins."""
        r = self._make(
            "google_service_account",
            name="projects/dev-proj-470211/serviceAccounts/"
                 "poc-sa@dev-proj-470211.iam.gserviceaccount.com",
            email="poc-sa@dev-proj-470211.iam.gserviceaccount.com",
            project="dev-proj-470211",
        )
        self.assertEqual(
            r.resource_name,
            "poc-sa@dev-proj-470211.iam.gserviceaccount.com",
            "SA must return the bare email, not the full canonical path",
        )

    def test_service_account_falls_back_to_name_when_email_missing(self):
        """Defensive: if email is somehow absent (corrupted state /
        partial import), fall back to `name`. Better to attempt a
        likely-broken describe than to skip the resource entirely."""
        r = self._make(
            "google_service_account",
            name="projects/p/serviceAccounts/sa@p.iam.gserviceaccount.com",
            project="p",
        )
        # Falls back to name (which gcloud will reject, but at least
        # the call is attempted).
        self.assertEqual(
            r.resource_name,
            "projects/p/serviceAccounts/sa@p.iam.gserviceaccount.com",
        )

    def test_service_account_email_empty_string_falls_through_to_name(self):
        """Edge case: empty-string email should be treated as missing."""
        r = self._make(
            "google_service_account",
            name="projects/p/serviceAccounts/sa@p.iam.gserviceaccount.com",
            email="",
            project="p",
        )
        # Empty string is falsy in Python, so falls back to `name`.
        self.assertEqual(
            r.resource_name,
            "projects/p/serviceAccounts/sa@p.iam.gserviceaccount.com",
        )

    # -- Edge case: missing name attribute --------------------------

    def test_returns_none_when_name_missing(self):
        """Resource with no `name` attribute returns None. Caller
        treats None as 'cannot describe; skip'."""
        r = self._make("google_compute_instance", project="p",
                       zone="us-central1-a")
        self.assertIsNone(r.resource_name)

    # -- Cross-check: SA exception doesn't bleed into other types ---

    def test_non_sa_type_with_email_attribute_still_uses_name(self):
        """Defensive: if a NON-SA resource type happened to have a
        top-level `email` attribute (unlikely but possible in some
        future GCP type), we must still use `name`. The SA carve-out
        is type-scoped, not attribute-scoped."""
        # Hypothetical: some future resource type with a top-level
        # email attribute that is NOT the gcloud describe key.
        r = self._make(
            "google_some_future_type",
            name="my-resource-name",
            email="contact@example.com",  # unrelated to describe
            project="p",
        )
        self.assertEqual(
            r.resource_name, "my-resource-name",
            "SA carve-out must be type-scoped; non-SA types must"
            " still use `name`",
        )


if __name__ == "__main__":
    unittest.main()
