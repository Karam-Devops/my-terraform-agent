# importer/tests/test_post_llm_overrides.py
"""P2-2 unit tests for the rename mechanism.

Until P2-2 the existing _rename_in_block (block-scoped renames like
`reservation_affinity.consume_reservation_type` -> `type`) wasn't
covered by unit tests. P2-2 adds a sibling _rename_at_top_level for
resource-body-root attributes (the `locations` -> `node_locations`
case for google_container_cluster). This file pins both helpers and
the apply_overrides dispatch logic, so a future refactor can't
regress either path silently.

Pure-string transforms; no schema oracle, no LLM, no I/O. Suitable
for unit testing without .terraform/ initialised.
"""

from __future__ import annotations

import unittest

from importer.post_llm_overrides import (
    _rename_at_top_level,
    _rename_in_block,
    apply_overrides,
    reset_cache,
)


class RenameAtTopLevelTests(unittest.TestCase):
    """Pin the resource-body-root rename behaviour added in P2-2."""

    def test_renames_root_attribute(self):
        """Real-world case: `locations` -> `node_locations` in a cluster."""
        hcl_in = (
            'resource "google_container_cluster" "x" {\n'
            '  location = "us-central1-a"\n'
            '  locations = ["us-central1-a", "us-central1-b"]\n'
            '}\n'
        )
        hcl_out, n = _rename_at_top_level(hcl_in, "locations", "node_locations")
        self.assertEqual(n, 1)
        self.assertIn('node_locations = ["us-central1-a"', hcl_out)
        # `location` (singular) must NOT have been touched.
        self.assertIn('location = "us-central1-a"', hcl_out)

    def test_does_not_rename_inside_string_value(self):
        """Field name appearing inside a string value must NOT match."""
        hcl_in = (
            'resource "x" "y" {\n'
            '  description = "valid locations to deploy to"\n'
            '}\n'
        )
        hcl_out, n = _rename_at_top_level(hcl_in, "locations", "node_locations")
        self.assertEqual(n, 0)
        self.assertEqual(hcl_out, hcl_in)

    def test_does_not_rename_as_suffix(self):
        """`locations` must not match inside `node_locations` (lookbehind guard)."""
        hcl_in = (
            'resource "x" "y" {\n'
            '  node_locations = ["a"]\n'
            '}\n'
        )
        hcl_out, n = _rename_at_top_level(hcl_in, "locations", "node_locations")
        self.assertEqual(n, 0)
        self.assertEqual(hcl_out, hcl_in)

    def test_does_not_rename_block_name(self):
        """Block declaration `name {` is not an attribute assignment."""
        hcl_in = (
            'resource "x" "y" {\n'
            '  locations {\n'  # would be a block, not an attribute
            '    enabled = true\n'
            '  }\n'
            '}\n'
        )
        hcl_out, n = _rename_at_top_level(hcl_in, "locations", "node_locations")
        # No `=` after `locations`, so the (\\s*=) anchor doesn't match.
        self.assertEqual(n, 0)
        self.assertEqual(hcl_out, hcl_in)

    def test_returns_zero_when_field_absent(self):
        """No-op on HCL that doesn't contain the source field."""
        hcl_in = 'resource "x" "y" {\n  zone = "us-central1-a"\n}\n'
        hcl_out, n = _rename_at_top_level(hcl_in, "locations", "node_locations")
        self.assertEqual(n, 0)
        self.assertEqual(hcl_out, hcl_in)


class RenameInBlockTests(unittest.TestCase):
    """Pin the existing block-scoped rename behaviour
    (regression coverage; previously untested)."""

    def test_renames_inside_named_block(self):
        """Real-world case: `consume_reservation_type` -> `type` in
        reservation_affinity."""
        hcl_in = (
            'resource "google_compute_instance" "x" {\n'
            '  name = "vm"\n'
            '  reservation_affinity {\n'
            '    consume_reservation_type = "ANY_RESERVATION"\n'
            '  }\n'
            '}\n'
        )
        hcl_out, n = _rename_in_block(
            hcl_in, "reservation_affinity",
            "consume_reservation_type", "type",
        )
        self.assertEqual(n, 1)
        self.assertIn("type = \"ANY_RESERVATION\"", hcl_out)
        self.assertNotIn("consume_reservation_type", hcl_out)

    def test_block_scoping_protects_other_occurrences(self):
        """The same field name elsewhere in the HCL must NOT be renamed."""
        hcl_in = (
            'resource "x" "y" {\n'
            '  consume_reservation_type = "ANY"\n'
            '  reservation_affinity {\n'
            '    consume_reservation_type = "SPECIFIC"\n'
            '  }\n'
            '}\n'
        )
        hcl_out, n = _rename_in_block(
            hcl_in, "reservation_affinity",
            "consume_reservation_type", "type",
        )
        # Only the one inside reservation_affinity {} is renamed.
        self.assertEqual(n, 1)
        # Top-level occurrence preserved unchanged.
        self.assertIn('consume_reservation_type = "ANY"', hcl_out)
        # Inside-block occurrence renamed.
        self.assertIn('type = "SPECIFIC"', hcl_out)


class ApplyOverridesDispatchTests(unittest.TestCase):
    """Pin the apply_overrides dispatch: empty block_path routes to
    top-level renamer; non-empty routes to block-scoped renamer.

    Both rename rules in post_llm_overrides.json (the existing
    reservation_affinity entry + the new top-level locations entry)
    exercise BOTH dispatch arms in the same .json -- this test verifies
    each arm is reachable.
    """

    def setUp(self):
        # Force cache miss so each test re-reads post_llm_overrides.json.
        # (The override cache is process-wide; bleeds across tests otherwise.)
        reset_cache()

    def tearDown(self):
        reset_cache()

    def test_top_level_rename_dispatch(self):
        """JSON entry with `block_path: ""` triggers _rename_at_top_level."""
        hcl_in = (
            'resource "google_container_cluster" "x" {\n'
            '  location = "us-central1-a"\n'
            '  locations = ["a", "b"]\n'
            '}\n'
        )
        hcl_out, corrections = apply_overrides("google_container_cluster", hcl_in)
        self.assertIn("node_locations", hcl_out)
        self.assertNotIn(" locations ", hcl_out)
        # Description includes <root> scope label so operators can
        # distinguish top-level from block-scoped renames in logs.
        self.assertTrue(
            any("<root>." in c for c in corrections),
            f"Expected <root>. in corrections; got {corrections}",
        )

    def test_block_scoped_rename_dispatch(self):
        """JSON entry with non-empty `block_path` uses _rename_in_block."""
        hcl_in = (
            'resource "google_compute_instance" "x" {\n'
            '  reservation_affinity {\n'
            '    consume_reservation_type = "ANY"\n'
            '  }\n'
            '}\n'
        )
        hcl_out, corrections = apply_overrides("google_compute_instance", hcl_in)
        self.assertIn("type =", hcl_out)
        self.assertNotIn("consume_reservation_type", hcl_out)
        # Description includes the block_path so it's distinguishable
        # from a root-level rename of the same field name.
        self.assertTrue(
            any("reservation_affinity." in c for c in corrections),
            f"Expected reservation_affinity. in corrections; got {corrections}",
        )

    def test_unknown_tf_type_returns_input_unchanged(self):
        """A tf_type not in the JSON config is a no-op."""
        hcl_in = 'resource "unmapped_type" "x" {\n  field = "value"\n}\n'
        hcl_out, corrections = apply_overrides("unmapped_type", hcl_in)
        self.assertEqual(hcl_out, hcl_in)
        self.assertEqual(corrections, [])


if __name__ == "__main__":
    unittest.main()
