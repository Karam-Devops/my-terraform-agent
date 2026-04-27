# detector/tests/test_rescan.py
"""P4-3 unit tests for detector.rescan -- the CG-1 unmanaged-resource
tracking entry point.

Mocks importer.inventory.inventory + state_reader.read_state so the
tests don't need live gcloud or a real terraform project. Coverage:
  * Diff correctness: cloud - state = unmanaged
  * Match rule (tf_type, normalized_short_name)
  * URN-style state names normalize via friendly_name_from_display
  * Empty-cloud + populated-state -> all compliant, zero unmanaged
  * Populated-cloud + empty-state -> all unmanaged
  * inventory_errors propagate to the report
  * project_root preflight: missing / non-existent -> PreflightError
"""

from __future__ import annotations

import os
import tempfile
import unittest
from unittest.mock import patch

from common.errors import PreflightError
from detector.rescan import rescan, _build_unmanaged
from detector.state_reader import ManagedResource
from importer.inventory import CloudResource


def _state(name: str, tf_type: str = "google_compute_instance") -> ManagedResource:
    return ManagedResource(
        tf_type=tf_type,
        hcl_name=name.replace("-", "_"),
        tf_address=f"{tf_type}.{name.replace('-', '_')}",
        attributes={"name": name},
        in_scope=True,
    )


def _cloud(name: str, tf_type: str = "google_compute_instance") -> CloudResource:
    return CloudResource(
        tf_type=tf_type,
        asset_type="compute.googleapis.com/Instance",
        cloud_name=name,
        cloud_urn=f"//compute.googleapis.com/projects/p/zones/us-central1-a/instances/{name}",
        project_id="dev-proj-470211",
    )


class BuildUnmanagedTests(unittest.TestCase):
    """The pure diff function -- no I/O. Most behavior tests live here
    because we don't need to mock anything."""

    def test_empty_cloud_yields_empty_unmanaged(self):
        # Project has state but cloud enumeration found nothing
        # (e.g. all resources got deleted out-of-band -- shows up as
        # missing-from-cloud, not unmanaged).
        result = _build_unmanaged([], [_state("vm-a")])
        self.assertEqual(result, [])

    def test_empty_state_yields_all_cloud_as_unmanaged(self):
        # No state file (fresh project) but cloud has resources -- all
        # of them are unmanaged.
        cloud = [_cloud("vm-a"), _cloud("vm-b")]
        result = _build_unmanaged(cloud, [])
        self.assertEqual(len(result), 2)
        self.assertEqual({c.cloud_name for c in result}, {"vm-a", "vm-b"})

    def test_perfect_overlap_yields_zero_unmanaged(self):
        # Every cloud resource has a matching state entry.
        cloud = [_cloud("vm-a"), _cloud("vm-b")]
        state = [_state("vm-a"), _state("vm-b")]
        result = _build_unmanaged(cloud, state)
        self.assertEqual(result, [])

    def test_partial_overlap_yields_only_missing(self):
        # State has vm-a + vm-b; cloud has vm-a + vm-b + rogue-bucket.
        # Only rogue-bucket is unmanaged.
        cloud = [
            _cloud("vm-a"),
            _cloud("vm-b"),
            _cloud("rogue-bucket", "google_storage_bucket"),
        ]
        state = [_state("vm-a"), _state("vm-b")]
        result = _build_unmanaged(cloud, state)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].cloud_name, "rogue-bucket")
        self.assertEqual(result[0].tf_type, "google_storage_bucket")

    def test_match_key_includes_tf_type(self):
        # Same name on different tf_types must NOT match.
        # E.g. `google_compute_instance.foo` and `google_storage_bucket.foo`
        # are different resources even though both are named "foo".
        cloud = [_cloud("foo", "google_storage_bucket")]
        state = [_state("foo", "google_compute_instance")]  # different tf_type
        result = _build_unmanaged(cloud, state)
        # The bucket is unmanaged because the state's "foo" is a VM.
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].tf_type, "google_storage_bucket")

    def test_urn_style_state_name_normalizes_for_match(self):
        # State stores some resource types with a URN as attributes.name
        # (KMS keyrings, Pub/Sub topics). The match key applies
        # friendly_name_from_display to both sides so URN <-> short
        # name compares correctly.
        cloud = [
            CloudResource(
                tf_type="google_kms_key_ring",
                asset_type="cloudkms.googleapis.com/KeyRing",
                cloud_name="poc-keyring",
                cloud_urn="...",
                project_id="dev-proj-470211",
            ),
        ]
        # State stores the URN form in name.
        state = [
            ManagedResource(
                tf_type="google_kms_key_ring",
                hcl_name="poc_keyring",
                tf_address="google_kms_key_ring.poc_keyring",
                attributes={"name": "projects/p/locations/us-central1/keyRings/poc-keyring"},
                in_scope=True,
            ),
        ]
        result = _build_unmanaged(cloud, state)
        # The keyring matches via normalized name -- no false unmanaged.
        self.assertEqual(result, [])

    def test_results_sorted_deterministically(self):
        cloud = [
            _cloud("z-vm", "google_compute_instance"),
            _cloud("a-bucket", "google_storage_bucket"),
            _cloud("a-vm", "google_compute_instance"),
        ]
        result = _build_unmanaged(cloud, [])
        labels = [(r.tf_type, r.cloud_name) for r in result]
        self.assertEqual(labels, [
            ("google_compute_instance", "a-vm"),
            ("google_compute_instance", "z-vm"),
            ("google_storage_bucket", "a-bucket"),
        ])


class RescanIntegrationTests(unittest.TestCase):
    """End-to-end test of rescan(): mocks the two I/O dependencies
    (inventory + read_state) and verifies the DriftReport shape."""

    def setUp(self):
        # rescan() requires project_root to be a real directory.
        self.tmpdir = tempfile.mkdtemp(prefix="rescan_test_")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_rescan_populates_three_buckets(self):
        cloud = [_cloud("vm-a"), _cloud("rogue-bucket", "google_storage_bucket")]
        state = [_state("vm-a")]
        with patch("detector.rescan._inventory", return_value=cloud), \
             patch("detector.rescan.state_reader.read_state",
                   return_value=state):
            report = rescan("dev-proj-470211", project_root=self.tmpdir)
        self.assertEqual(report.project_id, "dev-proj-470211")
        self.assertEqual(report.drifted, [])  # P4-3: not populated
        self.assertEqual(len(report.compliant), 1)
        self.assertEqual(report.compliant[0].tf_address,
                         "google_compute_instance.vm_a")
        self.assertEqual(len(report.unmanaged), 1)
        self.assertEqual(report.unmanaged[0].cloud_name, "rogue-bucket")

    def test_rescan_carries_inventory_errors(self):
        # Simulate inventory returning successfully but with errors --
        # for raise_on_error=False this isn't directly observable from
        # the return value (per inventory.py contract); we test the
        # catastrophic-failure recovery path instead.
        with patch("detector.rescan._inventory",
                   side_effect=RuntimeError("simulated catastrophic failure")), \
             patch("detector.rescan.state_reader.read_state",
                   return_value=[_state("vm-a")]):
            report = rescan("dev-proj-470211", project_root=self.tmpdir)
        # Recovery: empty cloud, the failure is recorded.
        self.assertEqual(len(report.unmanaged), 0)
        self.assertEqual(len(report.inventory_errors), 1)
        self.assertIn("inventory_call_failed", report.inventory_errors[0])
        # exit_code is non-zero because of the inventory error.
        self.assertEqual(report.exit_code, 1)

    def test_rescan_empty_project_clean_exit(self):
        # No cloud resources, no state -- fully clean.
        with patch("detector.rescan._inventory", return_value=[]), \
             patch("detector.rescan.state_reader.read_state",
                   return_value=[]):
            report = rescan("dev-proj-470211", project_root=self.tmpdir)
        self.assertEqual(report.exit_code, 0)
        self.assertEqual(report.total_in_state, 0)
        self.assertEqual(report.total_in_cloud, 0)


class RescanPreflightTests(unittest.TestCase):
    """rescan() is the new public entry point and must enforce the same
    preflight contract as detector.remediator (P4-1). No silent cwd
    fallback; missing project_root = PreflightError."""

    def test_none_project_root_raises_preflight_error(self):
        with self.assertRaises(PreflightError) as ctx:
            rescan("dev-proj-470211", project_root=None)  # type: ignore
        self.assertEqual(ctx.exception.stage, "resolve_workdir")

    def test_empty_project_root_raises_preflight_error(self):
        with self.assertRaises(PreflightError):
            rescan("dev-proj-470211", project_root="")

    def test_nonexistent_project_root_raises_preflight_error(self):
        bogus = os.path.join(tempfile.gettempdir(),
                             "definitely_does_not_exist_xyz_rescan_test")
        # Belt-and-braces: ensure it doesn't exist.
        if os.path.isdir(bogus):
            import shutil
            shutil.rmtree(bogus)
        with self.assertRaises(PreflightError) as ctx:
            rescan("dev-proj-470211", project_root=bogus)
        self.assertEqual(ctx.exception.stage, "resolve_workdir")
        self.assertIn("does not exist", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
