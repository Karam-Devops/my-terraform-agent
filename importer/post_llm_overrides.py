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
        print(f"[POST-LLM] Failed to load {_OVERRIDES_FILE}: {e}. Continuing with no overrides.")
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
            block_path = rename["block_path"]
            from_field = rename["from"]
            to_field = rename["to"]
        except (KeyError, TypeError) as e:
            print(f"[POST-LLM] Malformed rename entry in {tf_type} (missing {e}); skipped.")
            continue
        hcl_text, n = _rename_in_block(hcl_text, block_path, from_field, to_field)
        if n > 0:
            corrections.append(f"renamed '{block_path}.{from_field}' -> '{block_path}.{to_field}' ({n}x)")

    for deletion in rules.get("deletions", []):
        try:
            block_path = deletion["block_path"]
            field = deletion["field"]
        except (KeyError, TypeError) as e:
            print(f"[POST-LLM] Malformed deletion entry in {tf_type} (missing {e}); skipped.")
            continue
        hcl_text, n = _delete_in_block(hcl_text, block_path, field)
        if n > 0:
            corrections.append(f"deleted '{block_path}.{field}' ({n}x)")

    return hcl_text, corrections


def reset_cache() -> None:
    """Clear the cached overrides. Intended for tests; not needed in
    normal use."""
    global _cached_overrides
    _cached_overrides = None
