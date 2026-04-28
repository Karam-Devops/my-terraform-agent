# importer/_input.py
"""Programmatic-input helpers for ``importer.run.run_workflow`` (PUI-1).

run_workflow historically read its project_id and resource-selection
inputs from stdin via ``input()`` prompts. That's fine for the CLI but
breaks under Streamlit / Cloud Run where there is no terminal.

This module isolates the "where does the input come from?" decision:

  * ``_resolve_project_id_input(arg)`` -- CLI prompts via stdin when
    arg is None; UI passes the value through unchanged.
  * ``_resolve_selection_input(arg, all_discovered)`` -- CLI shows
    the interactive menu when arg is None; UI passes ``"all"`` (the
    PUI-1 v1 contract) or an explicit 1-indexed list.
  * ``_present_selection_menu(resources)`` -- the original interactive
    menu, kept here so the CLI fallback can call it without
    ``run_workflow`` re-implementing it.

Why a separate module rather than helpers in run.py:

  * run.py transitively imports ``llm_provider`` via
    ``hcl_generator``. That chain is fine at runtime but defeats
    pytest's import path -- ``from .. import llm_provider`` fails
    when ``importer`` is the top-level package (which is exactly the
    pytest layout). Splitting the input helpers off lets the unit
    tests import them in isolation without paying that import cost.
  * The helpers have no production dependency on the rest of run.py
    other than ``_present_selection_menu`` (which moves with them)
    and ``app_config`` (which is imported lazily inside the CLI
    branch -- the UI path never touches it).

Public surface: run.py re-imports these for its own use; nothing else
should consume them directly. The leading underscore on the module
name flags this as importer-internal API.
"""

from __future__ import annotations

from typing import List, Optional, Union


def _present_selection_menu(resources):
    """Render the historical interactive resource-selection menu.

    Reads from stdin via ``input()``. CLI-only -- the UI path bypasses
    this entirely by passing ``selected_indices="all"`` (or an explicit
    list) to ``run_workflow``.

    Returns the chosen subset of ``resources``, or [] if the operator
    entered ``0`` to cancel.

    Local-only sort by displayName for human-friendly menu order:
    the operator's muscle memory is alphabetical-by-name. Pre-PUI-1B
    this sort lived in run_workflow itself, but that re-ordered the
    list BEFORE selected_indices were applied -- causing the UI's
    indices (built from inventory()'s natural (tf_type, cloud_name)
    order) to point at different resources than the operator picked.
    Moving the sort here makes it CLI-display-only; UI gets the
    inventory() order verbatim, and the indices it sends actually
    match the rows it rendered.
    """
    sorted_for_menu = sorted(
        resources,
        key=lambda r: r.get('displayName', r.get('name', '')),
    )

    print("\n--- Stage 2: Select Resources to Import ---")
    for i, resource in enumerate(sorted_for_menu):
        display_name = resource.get('displayName', resource.get('name'))
        asset_type_short = resource.get('assetType').split('/')[-1]
        print(f"  [{i + 1}] {display_name:<40} (Type: {asset_type_short:<10})")

    while True:
        try:
            raw_input = input("\nEnter resource numbers separated by commas (e.g., 1, 5), or 0 to cancel: ")
            if raw_input.strip() == '0':
                return []
            choices = [int(i.strip()) for i in raw_input.split(',')]
            selected_assets = [
                sorted_for_menu[c - 1] for c in choices
                if 1 <= c <= len(sorted_for_menu)
            ]
            if selected_assets:
                return selected_assets
            else:
                print("❌ No valid selections made.")
        except ValueError:
            print("❌ Invalid input.")


def _resolve_project_id_input(
    arg: Optional[str],
    *,
    default_hint: str = "",
) -> str:
    """Return the raw project_id string. CLI prompts; UI passes through.

    Args:
        arg: If None, prompt the operator via stdin (CLI behaviour).
            If non-None, return it as-is for the caller to validate.
        default_hint: Pre-formatted text appended to the CLI prompt
            ("`` [<project>]``"). The caller computes this from
            ``app_config.config.TARGET_PROJECT_ID`` so this module
            doesn't need to import the broader importer chain.
            Ignored entirely on the UI path (arg != None).

    Returns:
        The raw project ID string (unvalidated -- caller runs it through
        ``app_config.resolve_target_project_id`` and surfaces errors as
        ``PreflightError``).
    """
    if arg is not None:
        return arg
    return input(f"Enter your Google Cloud Project ID{default_hint}: ")


def _resolve_selection_input(
    arg: Optional[Union[List[int], str]],
    all_discovered: list,
) -> list:
    """Resolve which discovered resources the workflow should import.

    Args:
        arg: One of:
            * ``None`` -- CLI: present the interactive selection menu.
            * ``"all"`` -- UI default: select every discovered resource.
                Phase 6 ships without a per-resource checkbox UI; the
                full-batch path is the v1 PUI-1 contract. A future
                PUI-6 polish task can add per-resource selection by
                passing an explicit index list instead.
            * ``list[int]`` -- 1-indexed positions from
                ``all_discovered`` (matches the CLI menu numbering so
                a customer-support handoff between UI / CLI uses the
                same indices). Out-of-range / non-int entries are
                silently dropped -- consistent with how the
                interactive menu handles them.
            * ``[]`` -- explicit empty list: treated as cancellation
                (zero-result, exit 0). Same outcome as the CLI menu's
                "0 to cancel" path.
        all_discovered: List of discovered resource dicts (the
            ``raw_asset`` form returned by ``inventory()``).

    Returns:
        The subset of ``all_discovered`` to import. Empty list signals
        cancellation; ``run_workflow`` returns a zeroed result.

    Raises:
        ValueError: ``arg`` is some other type / value. Caller bug
            -- fail fast rather than silently importing nothing.
    """
    if arg is None:
        return _present_selection_menu(all_discovered)
    if arg == "all":
        return list(all_discovered)
    if isinstance(arg, list):
        # 1-indexed; out-of-range / non-int entries silently dropped
        # (matches the CLI menu's tolerance for partial typos).
        return [
            all_discovered[i - 1]
            for i in arg
            if isinstance(i, int) and 1 <= i <= len(all_discovered)
        ]
    raise ValueError(
        f"selected_indices must be None, 'all', or a list of ints; "
        f"got {arg!r} (type {type(arg).__name__})"
    )
