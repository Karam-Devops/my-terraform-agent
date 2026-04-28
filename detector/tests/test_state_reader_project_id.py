# detector/tests/test_state_reader_project_id.py
"""Pin the ManagedResource.project_id resolution contract.

D-3 fix (2026-04-28): google_kms_crypto_key (and any future GCP type
that doesn't surface a top-level `project` attribute) used to trigger
a "no 'project' attribute in state. Skipping." warning + a missed
describe call. Tests below pin both the common case (top-level
`project` wins) AND the URN-extraction fallback for types that only
expose project via the `id` path.
"""

from __future__ import annotations

import unittest

from detector.state_reader import ManagedResource


class ManagedResourceProjectIdTests(unittest.TestCase):
    """Pin project_id resolution: top-level attr first, URN fallback."""

    def _make(self, tf_type: str = "google_dummy", **attrs) -> ManagedResource:
        return ManagedResource(
            tf_type=tf_type,
            hcl_name="x",
            tf_address=f"{tf_type}.x",
            attributes=attrs,
            in_scope=True,
        )

    # -- Common case: top-level `project` attribute -----------------

    def test_top_level_project_attribute_wins(self):
        """Most resource types (compute_instance, storage_bucket,
        firewall, etc.) have project at top level. Use it directly."""
        r = self._make(project="dev-proj-470211", name="x")
        self.assertEqual(r.project_id, "dev-proj-470211")

    def test_top_level_project_wins_even_when_id_present(self):
        """If both top-level `project` AND `id` are set, top-level
        wins (cheap happy path; no URN parsing needed)."""
        r = self._make(
            project="explicit-project",
            id="projects/different-from-id/zones/us-central1-a/disks/x",
            name="x",
        )
        self.assertEqual(r.project_id, "explicit-project")

    # -- D-3 fix: URN fallback via `id` -----------------------------

    def test_kms_crypto_key_extracts_project_from_id(self):
        """google_kms_crypto_key has no top-level `project` (encoded
        in parent `key_ring` URN). Extract from `id` which is
        `projects/<P>/locations/<L>/keyRings/<K>/cryptoKeys/<X>`."""
        r = self._make(
            tf_type="google_kms_crypto_key",
            name="poc-key",
            id="projects/dev-proj-470211/locations/us-central1/"
               "keyRings/poc-keyring/cryptoKeys/poc-key",
            key_ring="projects/dev-proj-470211/locations/us-central1/"
                     "keyRings/poc-keyring",
        )
        self.assertEqual(r.project_id, "dev-proj-470211")

    def test_pubsub_topic_url_extracts_project_from_id(self):
        """Hypothetical: a pubsub_topic with NO top-level project
        (defensive check for any future type that omits it). Pubsub's
        id pattern is `projects/<P>/topics/<T>`."""
        r = self._make(
            tf_type="google_pubsub_topic",
            name="my-topic",
            id="projects/dev-proj-470211/topics/my-topic",
        )
        self.assertEqual(r.project_id, "dev-proj-470211")

    # -- Edge cases / defensive ------------------------------------

    def test_returns_none_when_project_and_id_missing(self):
        """No project attribute, no id attribute -> None. Caller
        treats None as 'cannot determine project; skip describe'."""
        r = self._make(name="orphaned")
        self.assertIsNone(r.project_id)

    def test_returns_none_when_id_is_not_url_pattern(self):
        """Some types (e.g. google_storage_bucket) have id == bucket
        name (not a URN). Extraction returns None; caller falls back
        to other resolution paths."""
        r = self._make(name="my-bucket", id="my-bucket")
        # No top-level project, id doesn't start with `projects/`
        self.assertIsNone(r.project_id)

    def test_returns_none_when_id_is_empty_string(self):
        r = self._make(name="x", id="")
        self.assertIsNone(r.project_id)

    def test_returns_none_when_id_is_non_string(self):
        """Defensive: what if state is corrupted and id is None or
        a number? Don't crash."""
        r = self._make(name="x", id=None)
        self.assertIsNone(r.project_id)
        r2 = self._make(name="x", id=42)
        self.assertIsNone(r2.project_id)

    def test_extracts_project_from_id_with_minimal_path(self):
        """Edge case: id is the bare 'projects/<P>' with nothing
        after. Should still extract the project."""
        r = self._make(name="x", id="projects/dev-proj-470211")
        self.assertEqual(r.project_id, "dev-proj-470211")

    def test_returns_none_when_id_starts_with_projects_but_empty_segment(self):
        """Defensive: id is 'projects//<rest>' (empty project segment).
        Don't return empty string; fall through to None."""
        r = self._make(name="x", id="projects//topics/something")
        self.assertIsNone(r.project_id)


if __name__ == "__main__":
    unittest.main()
