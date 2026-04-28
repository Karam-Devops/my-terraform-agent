# importer/tests/test_run_workflow_programmatic.py
"""Unit tests for the PUI-1 programmatic-input helpers (Phase 6).

These cover the two helpers that decide where ``run_workflow``'s
project_id and resource-selection inputs come from:

  * ``_resolve_project_id_input(arg)`` -- CLI prompts via stdin when
    arg is None; UI passes the value through.
  * ``_resolve_selection_input(arg, all_discovered)`` -- CLI shows
    interactive menu when arg is None; UI passes ``"all"`` or a
    1-indexed list.

Why test the helpers in isolation rather than driving run_workflow
end-to-end: a full run_workflow exercise needs subprocess mocks for
terraform, gcloud, vertexai, plus a hydrated workdir. The helpers
are the only branching the PUI-1 refactor introduces; testing them
in isolation gives full coverage of the new behaviour with zero
mock surface beyond ``input``.

CLI backward-compat is the critical pin -- if either helper drops
the original interactive path, the CLI silently breaks. The first
test in each class asserts the fallback fires.
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

# Import directly from _input rather than via importer.run -- run.py
# transitively pulls in hcl_generator -> llm_provider, which only
# resolves cleanly when launched as part of the full importer chain
# (i.e. the actual CLI). _input was carved off precisely so unit
# tests for the programmatic-input branch don't pay that import cost.
from importer import _input as importer_input


class ResolveProjectIdInputTests(unittest.TestCase):
    """Pin the CLI / UI fork on _resolve_project_id_input."""

    def test_none_arg_falls_back_to_input_prompt(self):
        """CLI behaviour: no arg -> input() fires. Critical backward-
        compat pin -- the CLI smoke would silently break if we lost
        this branch."""
        with patch("builtins.input", return_value="dev-proj-470211") as mock_in:
            result = importer_input._resolve_project_id_input(None)
        self.assertEqual(result, "dev-proj-470211")
        mock_in.assert_called_once()

    def test_provided_arg_skips_input_prompt(self):
        """UI behaviour: arg provided -> input() never fires. This is
        what makes the workflow runnable from Cloud Run (no terminal)."""
        with patch("builtins.input") as mock_in:
            result = importer_input._resolve_project_id_input("dev-proj-470211")
        self.assertEqual(result, "dev-proj-470211")
        mock_in.assert_not_called()

    def test_provided_empty_string_passes_through(self):
        """An explicit empty string is the operator's responsibility --
        we don't second-guess by re-prompting. The downstream call
        (app_config.resolve_target_project_id) will raise on empty."""
        with patch("builtins.input") as mock_in:
            result = importer_input._resolve_project_id_input("")
        self.assertEqual(result, "")
        mock_in.assert_not_called()


class ResolveSelectionInputTests(unittest.TestCase):
    """Pin the CLI / UI fork on _resolve_selection_input."""

    def setUp(self):
        # Three fake resources -- shape matches what inventory()
        # returns (raw_asset dicts). The contents don't matter for
        # the selection-routing logic; only the position does.
        self.resources = [
            {"name": "vm-a", "displayName": "VM A"},
            {"name": "vm-b", "displayName": "VM B"},
            {"name": "vm-c", "displayName": "VM C"},
        ]

    def test_none_arg_falls_back_to_interactive_menu(self):
        """CLI: no arg -> interactive menu fires. Backward-compat pin."""
        with patch.object(
            importer_input, "_present_selection_menu",
            return_value=[self.resources[0]],
        ) as mock_menu:
            result = importer_input._resolve_selection_input(
                None, self.resources,
            )
        mock_menu.assert_called_once_with(self.resources)
        self.assertEqual(result, [self.resources[0]])

    def test_all_sentinel_selects_every_resource(self):
        """UI default: 'all' -> select everything discovered.
        This is the PUI-1 v1 contract -- per-resource picker is PUI-6."""
        with patch.object(importer_input, "_present_selection_menu") as mock_menu:
            result = importer_input._resolve_selection_input(
                "all", self.resources,
            )
        self.assertEqual(result, self.resources)
        mock_menu.assert_not_called()

    def test_all_sentinel_returns_a_copy_not_the_input(self):
        """The returned list should be independent of the input -- if a
        caller mutates it, all_discovered shouldn't change. Subtle but
        bites tests later if we ever return the same reference."""
        result = importer_input._resolve_selection_input(
            "all", self.resources,
        )
        self.assertIsNot(result, self.resources)
        # But the contents should be the same items (not deep-copied).
        self.assertEqual(result, self.resources)

    def test_explicit_indices_select_those_positions(self):
        """1-indexed positions matching the CLI menu numbering."""
        result = importer_input._resolve_selection_input(
            [1, 3], self.resources,
        )
        # 1-indexed: [1, 3] -> resources[0] and resources[2].
        self.assertEqual(result, [self.resources[0], self.resources[2]])

    def test_explicit_indices_drop_out_of_range(self):
        """Out-of-range silently dropped (matches CLI menu behaviour --
        '1, 99' returns just resource 1, not an error)."""
        result = importer_input._resolve_selection_input(
            [1, 99, 0, -5], self.resources,
        )
        # 0 is out-of-range (1-indexed); -5 is out-of-range; 99 is
        # out-of-range. Only 1 survives.
        self.assertEqual(result, [self.resources[0]])

    def test_explicit_indices_drop_non_int_entries(self):
        """Non-int entries are silently dropped. Defensive against UI
        passing strings if a form serialiser misbehaves."""
        result = importer_input._resolve_selection_input(
            [1, "2", None, 3.5], self.resources,
        )
        # Only the int 1 survives; "2" / None / 3.5 are dropped.
        self.assertEqual(result, [self.resources[0]])

    def test_empty_list_returns_empty(self):
        """Explicit cancellation: [] -> []. run_workflow treats this
        as the same outcome as the CLI menu's 'enter 0' path."""
        result = importer_input._resolve_selection_input(
            [], self.resources,
        )
        self.assertEqual(result, [])

    def test_invalid_arg_type_raises_value_error(self):
        """Type other than None/'all'/list -> ValueError. Caller bug."""
        with self.assertRaises(ValueError) as ctx:
            importer_input._resolve_selection_input(
                {"some": "dict"}, self.resources,
            )
        self.assertIn("must be None, 'all', or a list", str(ctx.exception))

    def test_invalid_string_arg_raises_value_error(self):
        """Sentinel must be exactly 'all'; typos raise."""
        with self.assertRaises(ValueError):
            importer_input._resolve_selection_input(
                "ALL", self.resources,  # uppercase rejected
            )

    def test_empty_discovered_with_all_sentinel_returns_empty(self):
        """Edge case: 'all' against an empty discovery list. Returns
        empty (caller treats as zero-selection -> zeroed result, exit 0)."""
        result = importer_input._resolve_selection_input("all", [])
        self.assertEqual(result, [])


if __name__ == "__main__":
    unittest.main()
