# importer/tests/test_golden_examples_loader.py
"""P4-9a tests for the CC-9 golden example loader.

Verifies:
  * Mode-specialized variants take precedence over default
  * Falls back to default when no mode variant exists
  * Returns None when nothing matches
  * format_example_section wraps content in the expected markers
  * The 3 P4-9a priority examples are present and parse-readable
"""

from __future__ import annotations

import os
import unittest

from importer.golden_examples_loader import (
    load_golden_example,
    format_example_section,
)


PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
GOLDEN_DIR = os.path.join(PROJECT_ROOT, "importer", "golden_examples")


class LoaderBehaviorTests(unittest.TestCase):

    def test_returns_none_for_unknown_type(self):
        # Defensive: no example for this type yet -> None, never raise.
        result = load_golden_example("google_definitely_not_a_real_type")
        self.assertIsNone(result)

    def test_returns_none_for_empty_tf_type(self):
        self.assertIsNone(load_golden_example(""))
        self.assertIsNone(load_golden_example(None))  # type: ignore

    def test_returns_default_when_no_mode_match(self):
        # cluster has gke_autopilot + gke_standard variants but no
        # default fallback file (intentional -- no sensible default
        # for cluster mode). Asking with an unrelated mode hint
        # returns None (no fallback).
        result = load_golden_example(
            "google_container_cluster",
            modes=["definitely_not_a_real_mode"],
        )
        # No default cluster file ships in P4-9a, so this is None.
        self.assertIsNone(result)

    def test_mode_variant_picked_when_available(self):
        # Autopilot mode -> picks the __gke_autopilot.tf variant.
        result = load_golden_example(
            "google_container_cluster",
            modes=["gke_autopilot"],
        )
        self.assertIsNotNone(result)
        # Verify it's the Autopilot variant by content signal
        # (Autopilot example has enable_autopilot = true).
        self.assertIn("enable_autopilot", result)

    def test_standard_variant_picked(self):
        result = load_golden_example(
            "google_container_cluster",
            modes=["gke_standard"],
        )
        self.assertIsNotNone(result)
        # Standard variant uses the remove_default_node_pool pattern
        # (positive identifier of the standard file). We don't assert
        # absence of enable_autopilot via substring because the
        # standard file's HEADER COMMENT mentions "NO enable_autopilot
        # = true" as part of its annotation -- testing for absence at
        # the substring level would catch the comment text. Real
        # behavioral verification of "doesn't enable autopilot"
        # belongs in P4-10 SMOKE on a real cluster.
        self.assertIn("remove_default_node_pool", result)

    def test_first_mode_wins_when_multiple_match(self):
        # If both modes have a variant, the first one in iteration
        # order wins. This is deterministic and matches the loader's
        # documented contract.
        result = load_golden_example(
            "google_container_cluster",
            modes=["gke_autopilot", "gke_standard"],
        )
        self.assertIn("enable_autopilot", result)

    def test_default_variant_for_cloudrun(self):
        # Cloud Run has no mode variants -- default file ships.
        result = load_golden_example("google_cloud_run_v2_service")
        self.assertIsNotNone(result)
        # Sanity: contains the v2 resource type.
        self.assertIn("google_cloud_run_v2_service", result)

    def test_modes_none_falls_back_to_default(self):
        # Passing modes=None (the default) skips the variant search
        # entirely and goes straight to <tf_type>.tf.
        result = load_golden_example("google_cloud_run_v2_service",
                                     modes=None)
        self.assertIsNotNone(result)


class FormatSectionTests(unittest.TestCase):
    """Verifies the prompt-injection wrapper format."""

    def test_wraps_with_clear_section_markers(self):
        formatted = format_example_section("resource \"foo\" \"bar\" {}")
        self.assertIn("--- REFERENCE EXAMPLE", formatted)
        self.assertIn("--- END REFERENCE EXAMPLE", formatted)

    def test_includes_dont_copy_verbatim_warning(self):
        # Critical: without the DO NOT COPY warning, the LLM may
        # echo back the example's literal identifiers (project ID,
        # bucket name, etc.). Pin the warning text so it can't be
        # silently removed in a refactor.
        formatted = format_example_section("dummy")
        self.assertIn("DO NOT", formatted)

    def test_includes_user_content(self):
        formatted = format_example_section("resource \"foo\" \"bar\" {}")
        self.assertIn("resource \"foo\" \"bar\" {}", formatted)


class GoldenFilesPresenceTests(unittest.TestCase):
    """P4-9a ships exactly 3 golden examples per the punchlist
    priority-3 list. Pin the floor so they can't be silently
    removed."""

    EXPECTED_FILES = (
        "google_container_cluster__gke_autopilot.tf",
        "google_container_cluster__gke_standard.tf",
        "google_cloud_run_v2_service.tf",
    )

    def test_all_priority_examples_exist(self):
        for fname in self.EXPECTED_FILES:
            with self.subTest(fname=fname):
                path = os.path.join(GOLDEN_DIR, fname)
                self.assertTrue(
                    os.path.isfile(path),
                    f"P4-9a priority golden example missing: {fname}",
                )

    def test_examples_are_non_empty(self):
        for fname in self.EXPECTED_FILES:
            with self.subTest(fname=fname):
                path = os.path.join(GOLDEN_DIR, fname)
                with open(path, "r", encoding="utf-8") as f:
                    content = f.read()
                self.assertGreater(
                    len(content.strip()), 100,
                    f"{fname} is suspiciously small (<100 chars); "
                    f"likely truncated or corrupted",
                )


if __name__ == "__main__":
    unittest.main()
