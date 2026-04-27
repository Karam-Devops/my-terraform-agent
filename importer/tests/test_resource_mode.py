# importer/tests/test_resource_mode.py
"""Unit tests for importer/resource_mode.py.

Pre-P2-9 the resource_mode module had no direct unit tests -- the
gke_autopilot mode was exercised only via integration through the
importer SMOKE. P2-9 adds a sibling `gke_standard` mode, doubles
the prune-list complexity of `gke_autopilot`, and surfaces
detection logic that's worth pinning at the unit level so future
mode additions don't silently break the existing ones.

Coverage focus:
  * Detector correctness: gke_autopilot AND gke_standard return
    the right boolean for the right snapshot shape.
  * Mutual exclusion: a single cluster snapshot triggers exactly
    one of the two cluster modes.
  * Prune list: P2-9's nodeLocations + advancedDatapathObservabilityConfig
    additions actually fire on the right snapshots.
  * Prompt addendum: gke_standard's addendum is non-empty so a
    future refactor can't silently delete it.
"""

from __future__ import annotations

import unittest

from importer.resource_mode import (
    _gke_is_autopilot,
    _gke_is_standard,
    apply_modes,
    detect_modes,
    mode_prompt_addendum,
)


class GkeModeDetectionTests(unittest.TestCase):
    """Pin the cluster-mode detectors. Mutual exclusion is the
    invariant: every cluster snapshot triggers exactly one of
    gke_autopilot or gke_standard, never both, never neither.
    """

    def test_autopilot_enabled_via_new_api_field(self):
        """`autopilotConfig.enabled = true` is the post-2024 API shape."""
        snap = {"autopilotConfig": {"enabled": True}}
        self.assertTrue(_gke_is_autopilot(snap))
        self.assertFalse(_gke_is_standard(snap))

    def test_autopilot_enabled_via_legacy_api_field(self):
        """`autopilot.enabled = true` is the older API shape; still
        accepted for back-compat with older gcloud versions."""
        snap = {"autopilot": {"enabled": True}}
        self.assertTrue(_gke_is_autopilot(snap))
        self.assertFalse(_gke_is_standard(snap))

    def test_standard_when_autopilot_field_absent(self):
        """No autopilot field at all -> Standard."""
        snap = {"name": "my-cluster", "location": "us-central1-a"}
        self.assertFalse(_gke_is_autopilot(snap))
        self.assertTrue(_gke_is_standard(snap))

    def test_standard_when_autopilot_disabled(self):
        """`autopilot.enabled = false` -> Standard (the field is
        present but cluster is in Standard mode)."""
        snap = {"autopilot": {"enabled": False}}
        self.assertFalse(_gke_is_autopilot(snap))
        self.assertTrue(_gke_is_standard(snap))

    def test_non_dict_input_returns_false_for_both(self):
        """Defensive: truly non-dict snapshots (None, list, str, int)
        return False from BOTH detectors -- safest behaviour, no mode
        fires on garbage input."""
        for bad in (None, [], "", 0):
            with self.subTest(value=bad):
                self.assertFalse(_gke_is_autopilot(bad))
                self.assertFalse(_gke_is_standard(bad))

    def test_dict_with_garbage_autopilot_value_is_standard(self):
        """Mutual-exclusion edge case: a dict snapshot whose
        `autopilot` key holds a non-dict value (e.g. a stray string
        from a malformed gcloud response) is treated as Standard.
        Justification: dict-with-garbage IS still a real cluster
        snapshot; absence of a parsable Autopilot flag means it's
        not Autopilot, which by mutual exclusion makes it Standard.
        Pinning this so future detector refactors don't accidentally
        flip Standard to False here and break the invariant."""
        snap = {"autopilot": "not_a_dict"}
        self.assertFalse(_gke_is_autopilot(snap))
        self.assertTrue(_gke_is_standard(snap))


class DetectModesTests(unittest.TestCase):
    """Public API: detect_modes returns the list of matching mode IDs."""

    def test_autopilot_snapshot_returns_only_gke_autopilot(self):
        snap = {"autopilotConfig": {"enabled": True}}
        modes = detect_modes(snap, "google_container_cluster")
        self.assertEqual(modes, ["gke_autopilot"])

    def test_standard_snapshot_returns_only_gke_standard(self):
        snap = {"name": "std-cluster"}
        modes = detect_modes(snap, "google_container_cluster")
        self.assertEqual(modes, ["gke_standard"])

    def test_non_cluster_type_returns_empty(self):
        """Modes only fire for the tf_type they `applies_to`.

        google_storage_bucket is the canonical "no modes registered"
        type today -- pin it explicitly so this test stays meaningful
        even as we add per-type default modes (P4-11 added
        cloud_run_v2_default; P4-13 added compute_instance_default;
        future commits may add others).
        """
        snap = {"autopilotConfig": {"enabled": True}}
        modes = detect_modes(snap, "google_storage_bucket")
        self.assertEqual(modes, [])

    def test_compute_instance_default_mode_always_fires(self):
        """P4-13: every google_compute_instance snapshot picks up the
        compute_instance_default mode for v1-vestige stripping
        (guest_os_features, resource_policies). Mirrors how
        cloud_run_v2_default works for google_cloud_run_v2_service."""
        modes = detect_modes({}, "google_compute_instance")
        self.assertIn("compute_instance_default", modes)


class P29PruneListTests(unittest.TestCase):
    """Pin the P2-9 prune-list additions on gke_autopilot:
    nodeLocations and advancedDatapathObservabilityConfig.
    """

    def test_autopilot_strips_advanced_datapath_observability_config(self):
        """P2-9.1 hotfix: this field is NESTED at
        monitoring_config.advanced_datapath_observability_config in the
        actual API response, NOT at the cluster's top level. The
        original P2-9 placement in `prune_top_level` was a no-op against
        real Autopilot snapshots; surfaced by SMOKE 2. Hotfix moved the
        entry to `prune_paths` where _strip_one_path actually matches it.
        """
        snap = {
            "name": "ap-cluster",
            "autopilotConfig": {"enabled": True},
            "monitoringConfig": {
                "componentConfig": {"enableComponents": ["SYSTEM_COMPONENTS"]},
                "advancedDatapathObservabilityConfig": {
                    "enableMetrics": True,
                    "relayMode": "INTERNAL_VPC_LB",
                },
            },
        }
        cleaned, dropped = apply_modes(dict(snap), ["gke_autopilot"])
        # The nested block is gone; the parent (monitoringConfig) survives
        # with its OTHER content intact.
        self.assertNotIn(
            "advancedDatapathObservabilityConfig",
            cleaned.get("monitoringConfig", {}),
        )
        # Sibling content under monitoringConfig is preserved -- we only
        # pruned the one nested path, not the parent.
        self.assertIn("componentConfig", cleaned.get("monitoringConfig", {}))
        # `dropped` reports the actual cloud-JSON path that was removed
        # (camelCase, since that's how it appeared in the snapshot).
        self.assertIn(
            "monitoringConfig.advancedDatapathObservabilityConfig",
            dropped,
        )

    def test_autopilot_strips_node_locations(self):
        """P2-2 addition; pinned here as the prune list grows."""
        snap = {
            "name": "ap-cluster",
            "autopilotConfig": {"enabled": True},
            "nodeLocations": ["us-central1-a", "us-central1-b"],
        }
        cleaned, dropped = apply_modes(dict(snap), ["gke_autopilot"])
        self.assertNotIn("nodeLocations", cleaned)
        self.assertIn("nodeLocations", dropped)

    def test_standard_does_not_strip_node_locations(self):
        """Symmetric: Standard mode does NOT prune nodeLocations,
        so it passes through untouched (the post-LLM rename
        from P2-2 then handles `locations` -> `node_locations`)."""
        snap = {
            "name": "std-cluster",
            "nodeLocations": ["us-central1-a"],
        }
        cleaned, dropped = apply_modes(dict(snap), ["gke_standard"])
        self.assertIn("nodeLocations", cleaned)
        self.assertEqual(dropped, [])

    def test_standard_strips_cluster_ipv4_cidr(self):
        """P2-10: SMOKE 2 surfaced
        `cluster_ipv4_cidr conflicts with ip_allocation_policy`.
        Modern Standard clusters always have ip_allocation_policy
        (VPC-native required since 2022), making cluster_ipv4_cidr
        redundant at the top-level. Strip it from Standard snapshots
        so the LLM never emits both mutually-exclusive fields."""
        snap = {
            "name": "std-cluster",
            "clusterIpv4Cidr": "10.36.0.0/14",
            "ipAllocationPolicy": {"useIpAliases": True},
        }
        cleaned, dropped = apply_modes(dict(snap), ["gke_standard"])
        self.assertNotIn("clusterIpv4Cidr", cleaned)
        self.assertIn("clusterIpv4Cidr", dropped)
        # ipAllocationPolicy survives -- the modern equivalent that
        # the LLM should write into HCL.
        self.assertIn("ipAllocationPolicy", cleaned)


class GkeNodePoolModeTests(unittest.TestCase):
    """P2-11 regression coverage for the new gke_node_pool mode.

    SMOKE 2 surfaced this:
        Error: Unsupported argument
        on google_container_node_pool_default_pool.tf line 39:
        39:       cgroup_mode = "CGROUP_MODE_V2"
        An argument named "cgroup_mode" is not expected here.

    cgroup_mode lives at node_config.linux_node_config.cgroup_mode in
    the schema, NOT directly in node_config. The LLM's nesting
    confusion needed prompt-level guidance.
    """

    def test_node_pool_mode_fires_on_any_node_pool_snapshot(self):
        """Detector is _always_true for the node pool resource type;
        every node pool snapshot triggers the addendum regardless of
        its content."""
        for snap in (
            {"name": "default-pool"},
            {"name": "pool-1", "config": {"machineType": "e2-medium"}},
            {},  # empty dict -- still a dict
        ):
            with self.subTest(snap=snap):
                modes = detect_modes(snap, "google_container_node_pool")
                self.assertEqual(modes, ["gke_node_pool"])

    def test_node_pool_mode_does_not_fire_on_clusters(self):
        """The mode is scoped via applies_to -- doesn't accidentally
        fire on cluster snapshots."""
        snap = {"name": "some-cluster"}
        modes = detect_modes(snap, "google_container_cluster")
        self.assertNotIn("gke_node_pool", modes)

    def test_node_pool_addendum_mentions_cgroup_mode_correct_nesting(self):
        """The specific SMOKE 2 failure case must be covered in the
        addendum text: cgroup_mode + linux_node_config."""
        addendum = mode_prompt_addendum(["gke_node_pool"])
        self.assertIn("cgroup_mode", addendum)
        self.assertIn("linux_node_config", addendum)


class P211AlwaysTrueDetectorTests(unittest.TestCase):
    """Pin the _always_true detector contract used by gke_node_pool."""

    def test_returns_true_for_any_dict(self):
        from importer.resource_mode import _always_true
        self.assertTrue(_always_true({}))
        self.assertTrue(_always_true({"name": "foo"}))
        self.assertTrue(_always_true({"a": 1, "b": 2}))

    def test_returns_false_for_non_dict(self):
        """Defensive: protect against non-dict input regardless of type."""
        from importer.resource_mode import _always_true
        for bad in (None, [], "", 0, 42, "string"):
            with self.subTest(value=bad):
                self.assertFalse(_always_true(bad))


class GkeStandardPromptAddendumTests(unittest.TestCase):
    """Pin that the gke_standard prompt addendum exists and is
    substantive. A future refactor that empties it would silently
    regress P2-9's LLM-guidance fix."""

    def test_addendum_present_and_substantive(self):
        addendum = mode_prompt_addendum(["gke_standard"])
        self.assertGreater(
            len(addendum), 500,
            "gke_standard addendum should be substantial; got "
            f"{len(addendum)} chars",
        )

    def test_addendum_mentions_logging_config_top_level_rule(self):
        """The two specific Phase 2 SMOKE failures the addendum was
        added to address: logging_config + node_kubelet_config."""
        addendum = mode_prompt_addendum(["gke_standard"])
        self.assertIn("logging_config", addendum)
        self.assertIn("node_kubelet_config", addendum)

    def test_no_addendum_for_no_modes(self):
        self.assertEqual(mode_prompt_addendum([]), "")


if __name__ == "__main__":
    unittest.main()
