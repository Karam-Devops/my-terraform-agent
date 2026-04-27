# detector/tests/test_drift_report.py
"""P4-3 unit tests for DriftReport dataclass.

Mirrors the test pattern of importer/tests/test_results.py and
translator/tests/test_results.py -- frozen dataclass shape, exit_code
semantics, as_fields() payload. The three result types are
deliberately parallel; this test file pins the symmetry so a future
maintainer changing one without changing the others sees the
divergence immediately.
"""

from __future__ import annotations

import unittest
from dataclasses import FrozenInstanceError

from detector.drift_report import DriftReport
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


class DriftReportShapeTests(unittest.TestCase):

    def test_default_is_empty(self):
        # Empty project / no inventory: zeros across the board.
        r = DriftReport(project_id="dev-proj-470211")
        self.assertEqual(r.drifted, [])
        self.assertEqual(r.compliant, [])
        self.assertEqual(r.unmanaged, [])
        self.assertEqual(r.inventory_errors, [])
        self.assertEqual(r.duration_s, 0.0)

    def test_carries_all_buckets(self):
        r = DriftReport(
            project_id="dev-proj-470211",
            drifted=[_state("vm-a")],
            compliant=[_state("vm-b"), _state("vm-c")],
            unmanaged=[_cloud("rogue-bucket", "google_storage_bucket")],
            inventory_errors=["compute.googleapis.com/Foo"],
            duration_s=12.3,
        )
        self.assertEqual(len(r.drifted), 1)
        self.assertEqual(len(r.compliant), 2)
        self.assertEqual(len(r.unmanaged), 1)
        self.assertEqual(len(r.inventory_errors), 1)
        self.assertEqual(r.duration_s, 12.3)

    def test_total_in_state_sums_drifted_plus_compliant(self):
        r = DriftReport(
            project_id="dev-proj-470211",
            drifted=[_state("a"), _state("b")],
            compliant=[_state("c")],
        )
        self.assertEqual(r.total_in_state, 3)

    def test_total_in_cloud_includes_unmanaged(self):
        r = DriftReport(
            project_id="dev-proj-470211",
            compliant=[_state("a"), _state("b")],
            unmanaged=[_cloud("c"), _cloud("d"), _cloud("e")],
        )
        self.assertEqual(r.total_in_cloud, 5)

    def test_is_frozen(self):
        r = DriftReport(project_id="dev-proj-470211")
        with self.assertRaises(FrozenInstanceError):
            r.duration_s = 1.0  # type: ignore[misc]


class DriftReportExitCodeTests(unittest.TestCase):
    """Exit code semantics mirror WorkflowResult / TranslationResult:
    0 iff fully clean; 1 if any bucket has problems."""

    def test_exit_code_zero_when_fully_clean(self):
        r = DriftReport(
            project_id="dev-proj-470211",
            compliant=[_state("a"), _state("b")],
        )
        self.assertEqual(r.exit_code, 0)

    def test_exit_code_zero_on_completely_empty_project(self):
        # No state, no cloud, no errors -- still clean.
        r = DriftReport(project_id="dev-proj-470211")
        self.assertEqual(r.exit_code, 0)

    def test_exit_code_nonzero_on_any_drift(self):
        r = DriftReport(
            project_id="dev-proj-470211",
            drifted=[_state("a")],
            compliant=[_state("b")],
        )
        self.assertEqual(r.exit_code, 1)

    def test_exit_code_nonzero_on_unmanaged(self):
        # The CG-1 capability: unmanaged is NOT silent OK -- it counts
        # as a finding the operator must review.
        r = DriftReport(
            project_id="dev-proj-470211",
            compliant=[_state("a")],
            unmanaged=[_cloud("rogue-bucket", "google_storage_bucket")],
        )
        self.assertEqual(r.exit_code, 1)

    def test_exit_code_nonzero_on_inventory_errors(self):
        # Partial inventory: even with zero unmanaged and zero drift,
        # incomplete enumeration = caller doesn't know if it's actually
        # clean. Non-zero exit forces review.
        r = DriftReport(
            project_id="dev-proj-470211",
            compliant=[_state("a")],
            inventory_errors=["compute.googleapis.com/Foo"],
        )
        self.assertEqual(r.exit_code, 1)


class DriftReportAsFieldsTests(unittest.TestCase):
    """as_fields() payload feeds structured-log emission. Keep counts +
    project_id + duration; OMIT the heavy per-bucket lists (per-resource
    detail logged via separate events during the rescan)."""

    def test_excludes_per_bucket_lists(self):
        r = DriftReport(
            project_id="dev-proj-470211",
            drifted=[_state("a")],
            compliant=[_state("b")],
            unmanaged=[_cloud("c", "google_storage_bucket")],
            inventory_errors=["compute.googleapis.com/Foo"],
            duration_s=5.5,
        )
        d = r.as_fields()
        # Lists not in payload (per WorkflowResult convention).
        self.assertNotIn("drifted", d)
        self.assertNotIn("compliant", d)
        self.assertNotIn("unmanaged", d)
        self.assertNotIn("inventory_errors", d)

    def test_includes_counts_and_metadata(self):
        r = DriftReport(
            project_id="dev-proj-470211",
            drifted=[_state("a")],
            compliant=[_state("b")],
            unmanaged=[_cloud("c", "google_storage_bucket")],
            inventory_errors=["compute.googleapis.com/Foo"],
            duration_s=5.5,
        )
        d = r.as_fields()
        self.assertEqual(d["project_id"], "dev-proj-470211")
        self.assertEqual(d["drifted_count"], 1)
        self.assertEqual(d["compliant_count"], 1)
        self.assertEqual(d["unmanaged_count"], 1)
        self.assertEqual(d["inventory_error_count"], 1)
        self.assertEqual(d["duration_s"], 5.5)
        # exit_code is convenient -- many dashboards key off it.
        self.assertEqual(d["exit_code"], 1)


if __name__ == "__main__":
    unittest.main()
