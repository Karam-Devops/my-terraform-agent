# importer/tests/test_post_llm_validation.py
"""P2-1 unit tests for the empty-block hallucination scrubber.

Pure-function tests with an injected fake oracle so they run without
.terraform/ initialised. The scrubber is the deterministic counterpart
to the LLM self-correction loop, so wrong behaviour here = silent HCL
mutilation. Pin both the detection regex and the schema-driven drop
decision rigorously.

Inspired by the Phase 1 SMOKE failures:
    pod_cidr_overprovision_config {}     -> requires `disabled`
    client_certificate_config {}         -> requires `issue_client_certificate`
    pubsub {}                            -> requires `enabled`
    advanced_datapath_observability_config {} -> requires `enable_relay`
All four would be caught by this layer.
"""

from __future__ import annotations

import unittest
from dataclasses import dataclass
from typing import List, Optional

from importer.post_llm_validation import (
    drop_required_field_empty_blocks,
    find_empty_blocks,
)


# ---------------------------------------------------------------------------
# Fake oracle for tests -- mimics the SchemaOracle public surface area we use
# (has, list_paths, get) without needing .terraform/ initialised.
# ---------------------------------------------------------------------------

@dataclass
class _FakeAttrInfo:
    path: str
    required: bool = False
    optional: bool = False
    computed: bool = False
    deprecated: bool = False


class _FakeOracle:
    """Minimal stand-in for SchemaOracle.

    `paths` maps tf_type -> {path -> _FakeAttrInfo}. Tests build the
    exact required/optional shape they need.
    """

    def __init__(self, paths: dict):
        self.paths = paths

    def has(self, tf_type: str) -> bool:
        return tf_type in self.paths

    def list_paths(self, tf_type: str, kind: Optional[str] = None) -> List[str]:
        return sorted(self.paths.get(tf_type, {}).keys())

    def get(self, tf_type: str, path: str):
        return self.paths.get(tf_type, {}).get(path)


# ---------------------------------------------------------------------------
# find_empty_blocks: pure detection regex
# ---------------------------------------------------------------------------

class FindEmptyBlocksTests(unittest.TestCase):
    """Pin the empty-block detection regex against real LLM output shapes."""

    def test_finds_simple_empty_block(self):
        hcl = "resource \"x\" \"y\" {\n  pubsub {}\n}\n"
        result = find_empty_blocks(hcl)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0][0], "pubsub")

    def test_finds_multi_line_empty_block(self):
        """Some LLMs emit `name {\\n}` as the empty form."""
        hcl = "resource \"x\" \"y\" {\n  pubsub {\n  }\n}\n"
        result = find_empty_blocks(hcl)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0][0], "pubsub")

    def test_finds_multiple_empty_blocks(self):
        hcl = (
            "resource \"x\" \"y\" {\n"
            "  pod_cidr_overprovision_config {}\n"
            "  client_certificate_config {}\n"
            "  pubsub {}\n"
            "}\n"
        )
        result = find_empty_blocks(hcl)
        names = sorted(name for name, _, _ in result)
        self.assertEqual(names, [
            "client_certificate_config",
            "pod_cidr_overprovision_config",
            "pubsub",
        ])

    def test_ignores_block_with_inner_field(self):
        """Non-empty blocks must not match -- we only target empties."""
        hcl = "resource \"x\" \"y\" {\n  pubsub {\n    enabled = true\n  }\n}\n"
        result = find_empty_blocks(hcl)
        self.assertEqual(result, [])

    def test_ignores_attribute_assignment(self):
        """`name = value` is not a block -- regex must not false-match."""
        hcl = "resource \"x\" \"y\" {\n  zone = \"us-central1-a\"\n}\n"
        result = find_empty_blocks(hcl)
        self.assertEqual(result, [])

    def test_finds_at_arbitrary_indent(self):
        """Nested empty blocks (any indent) should match too."""
        hcl = (
            "resource \"x\" \"y\" {\n"
            "  outer {\n"
            "      inner_empty {}\n"
            "  }\n"
            "}\n"
        )
        result = find_empty_blocks(hcl)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0][0], "inner_empty")


# ---------------------------------------------------------------------------
# drop_required_field_empty_blocks: schema-driven decision
# ---------------------------------------------------------------------------

class DropRequiredFieldEmptyBlocksTests(unittest.TestCase):
    """Pin the per-block keep/drop decision against the schema oracle."""

    def test_drops_block_with_required_inner_field(self):
        """Real-world case from SMOKE: pubsub {} but schema requires `enabled`."""
        oracle = _FakeOracle({
            "google_container_cluster": {
                "pubsub.enabled": _FakeAttrInfo("pubsub.enabled", required=True),
            },
        })
        hcl_in = "resource \"google_container_cluster\" \"x\" {\n  pubsub {}\n}\n"
        hcl_out, dropped = drop_required_field_empty_blocks(
            hcl_in, "google_container_cluster", oracle=oracle,
        )
        self.assertEqual(dropped, ["pubsub"])
        self.assertNotIn("pubsub", hcl_out)

    def test_keeps_block_with_only_optional_inner_fields(self):
        """master_auth {} on a GKE cluster: legitimate presence-only signal."""
        oracle = _FakeOracle({
            "google_container_cluster": {
                "master_auth.client_certificate_config":
                    _FakeAttrInfo("master_auth.client_certificate_config", optional=True),
            },
        })
        hcl_in = "resource \"google_container_cluster\" \"x\" {\n  master_auth {}\n}\n"
        hcl_out, dropped = drop_required_field_empty_blocks(
            hcl_in, "google_container_cluster", oracle=oracle,
        )
        self.assertEqual(dropped, [])
        self.assertIn("master_auth {}", hcl_out)

    def test_drops_only_required_field_blocks_from_mixed_set(self):
        """One drop-able + one keep-able coexisting in the same HCL."""
        oracle = _FakeOracle({
            "google_container_cluster": {
                "pubsub.enabled": _FakeAttrInfo("pubsub.enabled", required=True),
                "master_auth.client_certificate_config":
                    _FakeAttrInfo("master_auth.client_certificate_config", optional=True),
            },
        })
        hcl_in = (
            "resource \"google_container_cluster\" \"x\" {\n"
            "  pubsub {}\n"
            "  master_auth {}\n"
            "}\n"
        )
        hcl_out, dropped = drop_required_field_empty_blocks(
            hcl_in, "google_container_cluster", oracle=oracle,
        )
        self.assertEqual(dropped, ["pubsub"])
        self.assertNotIn("pubsub", hcl_out)
        self.assertIn("master_auth {}", hcl_out)

    def test_no_empty_blocks_returns_unchanged(self):
        """Happy path: well-formed HCL passes through untouched."""
        oracle = _FakeOracle({"google_container_cluster": {}})
        hcl_in = "resource \"google_container_cluster\" \"x\" {\n  name = \"foo\"\n}\n"
        hcl_out, dropped = drop_required_field_empty_blocks(
            hcl_in, "google_container_cluster", oracle=oracle,
        )
        self.assertEqual(hcl_out, hcl_in)
        self.assertEqual(dropped, [])

    def test_empty_input_returns_empty(self):
        """Defensive: empty HCL string short-circuits cleanly."""
        oracle = _FakeOracle({})
        hcl_out, dropped = drop_required_field_empty_blocks("", "anything", oracle=oracle)
        self.assertEqual(hcl_out, "")
        self.assertEqual(dropped, [])

    def test_unknown_tf_type_keeps_blocks(self):
        """If oracle has no schema for this type, fail-open: keep everything."""
        oracle = _FakeOracle({})
        hcl_in = "resource \"unknown_type\" \"x\" {\n  some_block {}\n}\n"
        hcl_out, dropped = drop_required_field_empty_blocks(
            hcl_in, "unknown_type", oracle=oracle,
        )
        self.assertEqual(dropped, [])
        self.assertIn("some_block {}", hcl_out)

    def test_dropped_list_is_sorted_and_deduped(self):
        """Two empty blocks of the same name -> single entry in dropped."""
        oracle = _FakeOracle({
            "x": {
                "pubsub.enabled": _FakeAttrInfo("pubsub.enabled", required=True),
            },
        })
        hcl_in = (
            "resource \"x\" \"y\" {\n"
            "  pubsub {}\n"
            "  pubsub {}\n"
            "}\n"
        )
        _, dropped = drop_required_field_empty_blocks(hcl_in, "x", oracle=oracle)
        self.assertEqual(dropped, ["pubsub"])

    def test_oracle_exception_returns_unchanged(self):
        """Oracle errors must not mutate the HCL -- fail-open contract."""

        class _BoomOracle:
            def has(self, tf_type):
                raise RuntimeError("oracle exploded")

        hcl_in = "resource \"x\" \"y\" {\n  pubsub {}\n}\n"
        hcl_out, dropped = drop_required_field_empty_blocks(
            hcl_in, "x", oracle=_BoomOracle(),
        )
        # has() raises in _block_has_required_inner_field's try-except
        # -> returns False -> block kept. Defensive contract holds.
        self.assertEqual(dropped, [])
        self.assertIn("pubsub {}", hcl_out)


class NestedEmptyBlockTests(unittest.TestCase):
    """P2-7 regression coverage for nested empty blocks.

    The Phase 2 SMOKE on dev-proj-470211 surfaced this case:
    `pubsub {}` emitted INSIDE `notification_config { ... }`. The
    pre-P2-7 scrubber only looked for `pubsub.<*>` paths in the
    schema (top-level pubsub block) and missed
    `notification_config.pubsub.enabled` -- so the empty block
    survived to terraform plan-verify, which rejected it.

    These tests pin the two-step lookup: prefer top-level matches
    when they exist, fall back to nested matches when they don't.
    """

    def test_drops_nested_block_with_required_inner_field(self):
        """The canonical P2-7 case: pubsub only nested in notification_config."""
        oracle = _FakeOracle({
            "google_container_cluster": {
                "notification_config.pubsub.enabled":
                    _FakeAttrInfo("notification_config.pubsub.enabled", required=True),
            },
        })
        hcl_in = (
            'resource "google_container_cluster" "x" {\n'
            '  notification_config {\n'
            '    pubsub {}\n'
            '  }\n'
            '}\n'
        )
        hcl_out, dropped = drop_required_field_empty_blocks(
            hcl_in, "google_container_cluster", oracle=oracle,
        )
        self.assertEqual(dropped, ["pubsub"])
        self.assertNotIn("pubsub {}", hcl_out)

    def test_keeps_nested_block_with_only_optional_fields(self):
        """Symmetric to the top-level case: nested block with only
        optional inner fields is a legitimate presence-only signal."""
        oracle = _FakeOracle({
            "google_container_cluster": {
                "addons_config.network_policy_config.disabled":
                    _FakeAttrInfo("addons_config.network_policy_config.disabled",
                                  optional=True),
            },
        })
        hcl_in = (
            'resource "google_container_cluster" "x" {\n'
            '  addons_config {\n'
            '    network_policy_config {}\n'
            '  }\n'
            '}\n'
        )
        hcl_out, dropped = drop_required_field_empty_blocks(
            hcl_in, "google_container_cluster", oracle=oracle,
        )
        self.assertEqual(dropped, [])
        self.assertIn("network_policy_config {}", hcl_out)

    def test_top_level_takes_priority_over_nested(self):
        """When both top-level and nested same-name paths exist, only
        top-level is consulted -- prevents cross-pollution false-positives.

        Schema declares BOTH:
          * top-level `master_auth.*` (all optional) -- legit presence-only
          * nested `<other_block>.master_auth.<required>` (hypothetical)

        Empty `master_auth {}` at any indent should be KEPT because the
        top-level paths declare all-optional inner fields. The nested
        path is irrelevant because top-level exists.
        """
        oracle = _FakeOracle({
            "google_container_cluster": {
                "master_auth.client_certificate_config":
                    _FakeAttrInfo("master_auth.client_certificate_config",
                                  optional=True),
                # Hypothetical cross-pollution: another block contains
                # a `master_auth` sub-key with a required field. Without
                # the two-step priority, a naive `.master_auth.` substring
                # match would drop a legit empty top-level master_auth {}.
                "some_other_block.master_auth.required_field":
                    _FakeAttrInfo("some_other_block.master_auth.required_field",
                                  required=True),
            },
        })
        hcl_in = (
            'resource "google_container_cluster" "x" {\n'
            '  master_auth {}\n'
            '}\n'
        )
        hcl_out, dropped = drop_required_field_empty_blocks(
            hcl_in, "google_container_cluster", oracle=oracle,
        )
        # Top-level exists with all-optional fields -> KEEP.
        # The nested `some_other_block.master_auth.required_field`
        # is correctly ignored because top-level paths exist.
        self.assertEqual(dropped, [])
        self.assertIn("master_auth {}", hcl_out)


if __name__ == "__main__":
    unittest.main()
