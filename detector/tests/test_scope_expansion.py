# detector/tests/test_scope_expansion.py
"""P4-4 (CG-2 part A) tests for the detector + policy scope expansion.

Verifies:
  * detector.config.IN_SCOPE_TF_TYPES auto-derives from importer config
    (so a new importer type flows in automatically without separate edit)
  * detector.config.DRIFT_AWARE_TF_TYPES stays at the original 2 types
    (the ones with full normalization rules)
  * is_in_scope() and is_drift_aware() predicates behave correctly
  * ResourceDrift.drift_stub field defaults False, has_drift returns
    False when drift_stub=True (so stub entries don't trigger
    remediation prompts)
  * policy.config.IN_SCOPE_TF_TYPES extends to all 17 GCP + 2 AWS
"""

from __future__ import annotations

import unittest

from detector import config as det_config
from detector.diff_engine import ResourceDrift
from importer import config as importer_config
from policy import config as policy_config


class DetectorScopeTests(unittest.TestCase):
    """detector/config.py: IN_SCOPE_TF_TYPES + DRIFT_AWARE_TF_TYPES +
    predicates."""

    def test_in_scope_covers_all_importer_types(self):
        # IN_SCOPE_TF_TYPES is auto-derived from importer config so the
        # detector and importer can never go out of sync. Adding a new
        # importer type later automatically extends detector coverage.
        self.assertEqual(
            det_config.IN_SCOPE_TF_TYPES,
            set(importer_config.TF_TYPE_TO_GCLOUD_INFO.keys()),
            "detector IN_SCOPE_TF_TYPES must auto-derive from importer "
            "config so the two stay in sync without manual coordination",
        )

    def test_in_scope_covers_at_least_17_types(self):
        # P4-4 baseline: importer supports 17 GCP types as of this commit.
        # The set may grow in future Phases; this test pins the FLOOR.
        self.assertGreaterEqual(
            len(det_config.IN_SCOPE_TF_TYPES), 17,
            "detector should be in-scope for at least 17 types as of P4-4",
        )

    def test_drift_aware_is_subset_of_in_scope(self):
        # Sanity invariant: every drift-aware type MUST also be
        # in-scope. Otherwise we'd have a type whose drift_engine has
        # rules but that the detector never visits -- dead code.
        self.assertTrue(
            det_config.DRIFT_AWARE_TF_TYPES <= det_config.IN_SCOPE_TF_TYPES,
            "every drift-aware type must also be in-scope",
        )

    def test_drift_aware_starts_at_two_types(self):
        # P4-4 baseline. As normalization rules ship for more types in
        # future commits, expand this set + the test.
        self.assertEqual(
            det_config.DRIFT_AWARE_TF_TYPES,
            {"google_compute_instance", "google_storage_bucket"},
        )

    def test_is_in_scope_predicate_returns_true_for_member(self):
        self.assertTrue(det_config.is_in_scope("google_compute_instance"))
        self.assertTrue(det_config.is_in_scope("google_kms_crypto_key"))
        self.assertTrue(det_config.is_in_scope("google_pubsub_topic"))

    def test_is_in_scope_predicate_returns_false_for_unknown(self):
        self.assertFalse(det_config.is_in_scope("aws_instance"))  # AWS not in detector scope
        self.assertFalse(det_config.is_in_scope("google_definitely_not_a_real_type"))

    def test_is_drift_aware_predicate_returns_true_for_drift_aware(self):
        self.assertTrue(det_config.is_drift_aware("google_compute_instance"))
        self.assertTrue(det_config.is_drift_aware("google_storage_bucket"))

    def test_is_drift_aware_predicate_returns_false_for_drift_stub(self):
        # Types in IN_SCOPE but not in DRIFT_AWARE -- the drift-stub set.
        self.assertFalse(det_config.is_drift_aware("google_kms_crypto_key"))
        self.assertFalse(det_config.is_drift_aware("google_container_cluster"))
        self.assertFalse(det_config.is_drift_aware("google_pubsub_topic"))


class ResourceDriftStubFieldTests(unittest.TestCase):
    """The new drift_stub field on ResourceDrift -- semantics for the
    drift-stub gating in detector.run.py."""

    def test_drift_stub_defaults_to_false(self):
        # Existing call sites that don't pass drift_stub should be
        # unchanged in behavior (back-compat).
        d = ResourceDrift(tf_address="x.y", tf_type="google_compute_instance")
        self.assertFalse(d.drift_stub)

    def test_has_drift_false_when_drift_stub_true(self):
        # The whole point of drift_stub: it does NOT trigger drift
        # alerts / remediation prompts. The UI shows "monitored,
        # checker conservative" instead.
        d = ResourceDrift(
            tf_address="x.y",
            tf_type="google_kms_crypto_key",
            drift_stub=True,
        )
        self.assertFalse(d.has_drift)

    def test_has_drift_false_when_drift_stub_true_even_with_items(self):
        # Belt-and-braces: even if a caller mistakenly populates
        # `items` AND `drift_stub`, the stub flag wins. Stub means
        # "we did NOT do a real diff" -- any items would be garbage.
        from detector.diff_engine import DriftItem
        d = ResourceDrift(
            tf_address="x.y",
            tf_type="google_kms_crypto_key",
            items=[DriftItem(path="foo", op="changed",
                             state_value=1, cloud_value=2)],
            drift_stub=True,
        )
        self.assertFalse(d.has_drift)

    def test_has_drift_true_for_normal_drift(self):
        # Sanity: drift-aware types with real items still report drift.
        from detector.diff_engine import DriftItem
        d = ResourceDrift(
            tf_address="x.y",
            tf_type="google_compute_instance",
            items=[DriftItem(path="foo", op="changed",
                             state_value=1, cloud_value=2)],
            drift_stub=False,
        )
        self.assertTrue(d.has_drift)


class PolicyScopeTests(unittest.TestCase):
    """policy/config.py:IN_SCOPE_TF_TYPES extends to the importer's
    full GCP coverage + the existing AWS pair."""

    def test_policy_in_scope_covers_all_importer_gcp_types(self):
        for tf_type in importer_config.TF_TYPE_TO_GCLOUD_INFO:
            with self.subTest(tf_type=tf_type):
                self.assertIn(
                    tf_type, policy_config.IN_SCOPE_TF_TYPES,
                    f"policy enforcer should cover {tf_type} -- the "
                    f"importer supports it; per-type rules ship in "
                    f"P4-5/6/7 but common/* rules apply immediately",
                )

    def test_policy_in_scope_keeps_aws_types(self):
        # AWS scope unchanged: aws_instance + aws_s3_bucket from P3-4.
        self.assertIn("aws_instance", policy_config.IN_SCOPE_TF_TYPES)
        self.assertIn("aws_s3_bucket", policy_config.IN_SCOPE_TF_TYPES)


if __name__ == "__main__":
    unittest.main()
