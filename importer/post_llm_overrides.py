# importer/post_llm_overrides.py
"""
Deterministic post-processing of LLM-generated HCL to correct known
provider field-name hallucinations.

Why this exists
---------------
LLM training data lags behind provider schemas. The model has memorized
old field names that the provider has since renamed -- e.g.
`reservation_affinity.consume_reservation_type` was renamed to plain
`type` in a recent google provider release. When asked to generate HCL
for the current schema, the LLM sometimes emits the old name because
that's what's most heavily represented in its training corpus.

Self-correction via `terraform validate` does eventually fix these,
but each retry costs an LLM call AND can re-trigger the same bug on
regeneration (whack-a-mole). A deterministic post-pass kills the bug
class entirely with zero LLM cost and zero non-determinism.

When to add an entry here
-------------------------
Add an entry when:
  1. You see the same hallucination across multiple importer runs.
  2. The fix is mechanical (rename A -> B within block X), not semantic.
  3. The provider schema disagrees with the LLM's chosen name.

Do NOT add an entry for one-off mistakes the LLM only makes once.
This file is for systematic, reproducible corrections; everything else
belongs in the self-correction retry loop.

Override types supported
------------------------
  - renames:   rename a field within a specific block-path scope
  - deletions: delete a `field = value` line within a block-path scope

Block scoping is critical: a field named `type` in `reservation_affinity`
is a different field than `type` in `disk` or `network_interface`.
Renames apply only within the named block.

Failure mode
------------
Fail-OPEN. If the override JSON file is missing, malformed, or any
individual entry is malformed, the affected entries are skipped with a
warning and the importer continues. We never crash because of this
layer; the worst case is the LLM's original HCL passes through
untouched and the existing self-correction loop catches the bug
through the LLM-and-validate path that worked before this file existed.
"""

from __future__ import annotations

import json
import os
import re
from typing import List, Tuple

from common.logging import get_logger

_log = get_logger(__name__)

_OVERRIDES_FILE = os.path.join(os.path.dirname(__file__), "post_llm_overrides.json")
_cached_overrides = None


def _load_overrides() -> dict:
    """Load the override map. Cached for process lifetime to avoid
    re-reading the JSON for every resource in a multi-resource scan.
    Returns {} on any failure so callers always get a usable dict."""
    global _cached_overrides
    if _cached_overrides is not None:
        return _cached_overrides
    if not os.path.exists(_OVERRIDES_FILE):
        _cached_overrides = {}
        return _cached_overrides
    try:
        with open(_OVERRIDES_FILE, "r", encoding="utf-8") as f:
            _cached_overrides = json.load(f)
    except (IOError, json.JSONDecodeError) as e:
        # Fail-open: importer continues with the LLM's raw HCL; the
        # self-correction loop catches any bug the overrides would have
        # masked. See module docstring "Failure mode" section.
        _log.warning(
            "post_llm_overrides_load_failed",
            path=_OVERRIDES_FILE,
            error=str(e),
            fallback="no_overrides_applied",
        )
        _cached_overrides = {}
    return _cached_overrides


def _find_top_block_ranges(hcl: str, block_name: str) -> List[Tuple[int, int]]:
    """Find every (start, end) interior range of `block_name { ... }`
    in `hcl`. `start` is the first char INSIDE the opening brace; `end`
    is the position of the matching closing brace.

    Uses brace-depth tracking so nested blocks of any depth resolve
    correctly. The lookbehind `(?<![A-Za-z0-9_])` prevents matching
    `block_name` as a suffix of a longer identifier (e.g. searching for
    `disk` should not match inside `boot_disk`).
    """
    ranges: List[Tuple[int, int]] = []
    pattern = re.compile(rf'(?<![A-Za-z0-9_]){re.escape(block_name)}\s*\{{')
    pos = 0
    while True:
        m = pattern.search(hcl, pos)
        if not m:
            break
        depth = 1
        i = m.end()
        while i < len(hcl) and depth > 0:
            ch = hcl[i]
            if ch == '{':
                depth += 1
            elif ch == '}':
                depth -= 1
            i += 1
        if depth != 0:
            # Unmatched brace -- HCL is malformed or our brace counter
            # got fooled by a `{` inside a string. Either way, give up
            # rather than make a wrong substitution.
            break
        ranges.append((m.end(), i - 1))
        pos = i
    return ranges


def _find_block_path_ranges(hcl: str, block_path: str) -> List[Tuple[int, int]]:
    """Resolve a dotted path like `boot_disk.initialize_params` into a
    list of interior ranges of the innermost block. At each step we
    narrow the search to the interior of the parent block found in the
    previous step."""
    parts = block_path.split('.')
    candidates: List[Tuple[int, int]] = [(0, len(hcl))]
    for part in parts:
        new_candidates: List[Tuple[int, int]] = []
        for s, e in candidates:
            sub = hcl[s:e]
            for inner_s, inner_e in _find_top_block_ranges(sub, part):
                # Translate inner offsets back to absolute hcl offsets.
                new_candidates.append((s + inner_s, s + inner_e))
        candidates = new_candidates
        if not candidates:
            return []
    return candidates


def _rename_in_block(hcl: str, block_path: str, from_field: str, to_field: str) -> Tuple[str, int]:
    """Rename `from_field` -> `to_field` within every occurrence of
    `block_path`. Processes ranges in reverse offset order so earlier
    range indices stay valid as we mutate the string from the end."""
    ranges = sorted(_find_block_path_ranges(hcl, block_path), reverse=True)
    # The trailing `(\s*=)` group ensures we only match `field =`
    # assignment positions, not bare references in comments or strings.
    field_pattern = re.compile(rf'(?<![A-Za-z0-9_]){re.escape(from_field)}(\s*=)')
    total = 0
    for start, end in ranges:
        block_text = hcl[start:end]
        new_text, n = field_pattern.subn(f'{to_field}\\1', block_text)
        if n > 0:
            hcl = hcl[:start] + new_text + hcl[end:]
            total += n
    return hcl, total


def _rename_at_top_level(hcl: str, from_field: str, to_field: str) -> Tuple[str, int]:
    """Rename `from_field` -> `to_field` for resource-body root attributes.

    Counterpart to `_rename_in_block` for the case where the rename
    target lives DIRECTLY inside the `resource "type" "name" { ... }`
    declaration, not inside a sub-block. Operates on the entire HCL
    text. The (?<![A-Za-z0-9_]) lookbehind prevents matching as a
    suffix of a longer identifier; the (\\s*=) capture restricts the
    rename to attribute-assignment positions (so we don't accidentally
    rewrite occurrences inside string values, comments, or the block
    name of a nested block).

    The mechanism is dispatched in `apply_overrides` when an entry
    has an empty / missing `block_path`. Caller need not know about
    this helper -- they just configure the rename in
    post_llm_overrides.json with `block_path: ""` (or omit the field).

    Surfaced by P2-2: cluster HCL emitted by the LLM uses `locations`
    (resource-body root attribute) instead of `node_locations`, and
    the existing block-scoped renamer can't reach root-level fields.
    """
    field_pattern = re.compile(rf'(?<![A-Za-z0-9_]){re.escape(from_field)}(\s*=)')
    new_text, n = field_pattern.subn(f'{to_field}\\1', hcl)
    return new_text, n


def _delete_in_block(hcl: str, block_path: str, field: str) -> Tuple[str, int]:
    """Delete the entire `field = value` line within every occurrence
    of `block_path`. Multi-line string values are out of scope -- LLM
    hallucinations are almost always single-line scalar assignments,
    and trying to handle heredocs here adds parser complexity for ~zero
    real-world benefit."""
    ranges = sorted(_find_block_path_ranges(hcl, block_path), reverse=True)
    line_pattern = re.compile(
        rf'^[ \t]*{re.escape(field)}\s*=\s*[^\n]*\n',
        re.MULTILINE,
    )
    total = 0
    for start, end in ranges:
        block_text = hcl[start:end]
        new_text, n = line_pattern.subn('', block_text)
        if n > 0:
            hcl = hcl[:start] + new_text + hcl[end:]
            total += n
    return hcl, total


def _delete_at_top_level(hcl: str, field: str) -> Tuple[str, int]:
    """Delete every `field = value` line from the entire HCL text.

    Counterpart to `_delete_in_block` for the case where a hallucinated
    field can appear at multiple block-path locations within the same
    resource body and the field has NO valid placement anywhere in the
    target schema (so deleting all occurrences is strictly safe).

    USE WITH CARE. This is the right call when:
      * The field is a v1-schema vestige the LLM mis-emitted on a v2
        resource (e.g. `container_concurrency` on
        `google_cloud_run_v2_service` -- v1 had it on
        `template.spec.container_concurrency`; v2 uses
        `template.max_instance_request_concurrency`. The legacy name
        is invalid ANYWHERE in the v2 schema.)
      * The field is a deprecated/removed identifier that the LLM
        learned from older training data.

    DO NOT use this when:
      * The field is valid in some block-path locations and invalid
        in others -- use _delete_in_block with the specific
        block_path instead.

    Dispatched in `apply_overrides` when a deletion entry has an
    empty / missing `block_path` (P2-8 -- mirror of P2-2's
    top-level rename dispatch).
    """
    line_pattern = re.compile(
        rf'^[ \t]*{re.escape(field)}\s*=\s*[^\n]*\n',
        re.MULTILINE,
    )
    new_text, n = line_pattern.subn('', hcl)
    return new_text, n


def apply_overrides(tf_type: str, hcl_text: str) -> Tuple[str, List[str]]:
    """Apply known LLM hallucination corrections to generated HCL.

    Returns:
        (corrected_hcl, list_of_correction_descriptions)
    The list is empty when no overrides applied (the common case).

    Per-entry try/except: a malformed override entry is logged and
    skipped, but other entries in the same resource type still apply.
    Per-tf_type miss: returns the input unchanged with no corrections.
    """
    overrides = _load_overrides()
    rules = overrides.get(tf_type)
    if not rules:
        return hcl_text, []

    corrections: List[str] = []

    for rename in rules.get("renames", []):
        try:
            # block_path is OPTIONAL since P2-2: empty/missing means the
            # rename targets a resource-body root attribute (no enclosing
            # nested block). `from` and `to` are required.
            block_path = rename.get("block_path", "")
            from_field = rename["from"]
            to_field = rename["to"]
        except (KeyError, TypeError) as e:
            _log.warning(
                "post_llm_override_malformed",
                kind="rename",
                tf_type=tf_type,
                missing_key=str(e),
            )
            continue
        if block_path:
            hcl_text, n = _rename_in_block(hcl_text, block_path, from_field, to_field)
            scope_label = f"{block_path}."
        else:
            hcl_text, n = _rename_at_top_level(hcl_text, from_field, to_field)
            scope_label = "<root>."
        if n > 0:
            corrections.append(
                f"renamed '{scope_label}{from_field}' -> '{scope_label}{to_field}' ({n}x)"
            )

    for deletion in rules.get("deletions", []):
        try:
            # block_path is OPTIONAL since P2-8: empty/missing means the
            # field is deleted from anywhere in the resource body (use
            # only when the field has NO valid placement in the schema).
            # `field` is required.
            block_path = deletion.get("block_path", "")
            field = deletion["field"]
        except (KeyError, TypeError) as e:
            _log.warning(
                "post_llm_override_malformed",
                kind="deletion",
                tf_type=tf_type,
                missing_key=str(e),
            )
            continue
        if block_path:
            hcl_text, n = _delete_in_block(hcl_text, block_path, field)
            scope_label = f"{block_path}."
        else:
            hcl_text, n = _delete_at_top_level(hcl_text, field)
            scope_label = "<root>."
        if n > 0:
            corrections.append(f"deleted '{scope_label}{field}' ({n}x)")

    return hcl_text, corrections


def reset_cache() -> None:
    """Clear the cached overrides. Intended for tests; not needed in
    normal use."""
    global _cached_overrides
    _cached_overrides = None
