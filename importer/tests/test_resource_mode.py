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
        """Modes only fire for the tf_type they `applies_to`."""
        snap = {"autopilotConfig": {"enabled": True}}
        modes = detect_modes(snap, "google_compute_instance")
        self.assertEqual(modes, [])


class P29PruneListTests(unittest.TestCase):
    """Pin the P2-9 prune-list additions on gke_autopilot:
    nodeLocations and advancedDatapathObservabilityConfig.
    """

    def test_autopilot_strips_advanced_datapath_observability_config(self):
        snap = {
            "name": "ap-cluster",
            "autopilotConfig": {"enabled": True},
            "advancedDatapathObservabilityConfig": {"enableMetrics": True},
        }
        cleaned, dropped = apply_modes(dict(snap), ["gke_autopilot"])
        self.assertNotIn("advancedDatapathObservabilityConfig", cleaned)
        self.assertIn("advancedDatapathObservabilityConfig", dropped)

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
        """Symmetric: Standard mode has empty prune_top_level, so
        nodeLocations passes through untouched (the post-LLM rename
        from P2-2 then handles `locations` -> `node_locations`)."""
        snap = {
            "name": "std-cluster",
            "nodeLocations": ["us-central1-a"],
        }
        cleaned, dropped = apply_modes(dict(snap), ["gke_standard"])
        self.assertIn("nodeLocations", cleaned)
        self.assertEqual(dropped, [])


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
