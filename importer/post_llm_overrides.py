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


def _inject_lifecycle_ignore_changes(
    hcl: str, fields: List[str],
) -> Tuple[str, int]:
    """Inject (or merge into) a `lifecycle { ignore_changes = [...] }`
    block on the resource's outermost body.

    PUI-1F v3.3 Fix 2 (2026-04-29 smoke 5): GENUINE fix for the class
    of bug where ``terraform import`` populates state with a value the
    LLM had no way to know about (server-stamped metadata like
    ``client``, ``client_version``; or provider-import-quirks like
    ``compute_disk.architecture`` where state ends up out-of-sync with
    cloud).

    Why post-LLM injection rather than asking the LLM via IGNORE_LIST:
    the LLM may decide to skip writing the block if it doesn't see the
    fields in the snapshot ("why ignore something I'm not writing?").
    Post-LLM injection is deterministic -- happens regardless of LLM
    cooperation.

    Why this is GENUINE (not a workaround):
      * cloud_run.client/client_version: server-stamped audit metadata,
        NEVER operator-configurable intent. ignoring is correct.
      * compute_disk.architecture: HCL retains ``architecture = "X86_64"``
        (no info loss), terraform's ignore_changes prevents the
        false-replacement diff caused by import-quirk in state.

    Behavior:
      * If the resource body has NO ``lifecycle`` block, append a new
        one with ``ignore_changes = [field1, field2, ...]``.
      * If a ``lifecycle`` block exists with ``ignore_changes``,
        merge (dedup) the new fields into the existing list.
      * If a ``lifecycle`` block exists WITHOUT ``ignore_changes``,
        add ``ignore_changes`` inside it.

    Returns (new_hcl, fields_added_count). 0 means already covered
    (idempotent) or HCL malformed (logged + skipped).
    """
    if not fields:
        return hcl, 0

    # Step 1: locate the outermost resource body. Match the
    # `resource "type" "name" {` opener and find the matching close.
    resource_match = re.search(
        r'^resource\s+"[^"]+"\s+"[^"]+"\s*\{',
        hcl,
        re.MULTILINE,
    )
    if not resource_match:
        return hcl, 0  # not a resource HCL; nothing to inject into

    body_start = resource_match.end()
    depth = 1
    body_end = body_start
    while body_end < len(hcl) and depth > 0:
        c = hcl[body_end]
        if c == '{':
            depth += 1
        elif c == '}':
            depth -= 1
            if depth == 0:
                break
        body_end += 1
    if depth != 0:
        return hcl, 0  # malformed HCL (unbalanced braces)

    body = hcl[body_start:body_end]

    # Step 2: detect existing lifecycle block by scanning the body
    # for `lifecycle {` at depth 0 (= direct child of the resource
    # body, not nested inside another block).
    lifecycle_pos = None
    body_depth = 0
    i = 0
    while i < len(body):
        # Match `lifecycle\s*{` at depth 0 only.
        if (body_depth == 0
            and body[i:i + 9] == "lifecycle"
            # word-boundary on left
            and (i == 0 or not body[i - 1].isalnum() and body[i - 1] != '_')):
            j = i + 9
            while j < len(body) and body[j] in ' \t':
                j += 1
            if j < len(body) and body[j] == '{':
                lifecycle_pos = (i, j + 1)  # (kw_start, body_start)
                break
        if body[i] == '{':
            body_depth += 1
        elif body[i] == '}':
            body_depth -= 1
        i += 1

    if lifecycle_pos is None:
        # No lifecycle block. Append one to the end of the resource body.
        # 2-space indent matches the existing convention used by the
        # LLM-generated HCL (see any successful .tf in the workdir).
        ic_lines = "\n".join(f"      {f}," for f in fields)
        new_block = (
            f"\n  lifecycle {{\n"
            f"    ignore_changes = [\n"
            f"{ic_lines}\n"
            f"    ]\n"
            f"  }}\n"
        )
        new_hcl = hcl[:body_end] + new_block + hcl[body_end:]
        return new_hcl, len(fields)

    # Step 3: existing lifecycle block. Find the matching close
    # within `body` (in body-relative coordinates, then translate).
    lc_kw_start, lc_body_start = lifecycle_pos
    lc_depth = 1
    lc_body_end = lc_body_start
    while lc_body_end < len(body) and lc_depth > 0:
        c = body[lc_body_end]
        if c == '{':
            lc_depth += 1
        elif c == '}':
            lc_depth -= 1
            if lc_depth == 0:
                break
        lc_body_end += 1
    if lc_depth != 0:
        return hcl, 0  # malformed lifecycle block

    lc_body = body[lc_body_start:lc_body_end]
    ignore_match = re.search(
        r'ignore_changes\s*=\s*\[([^\]]*)\]',
        lc_body,
        re.DOTALL,
    )

    if ignore_match:
        # Merge: parse existing identifiers, union with new fields.
        existing_str = ignore_match.group(1)
        existing_fields = set(re.findall(r'\b[a-zA-Z_][a-zA-Z0-9_]*\b',
                                          existing_str))
        new_fields_set = set(fields) - existing_fields
        if not new_fields_set:
            return hcl, 0  # already covered (idempotent)
        merged = sorted(existing_fields | set(fields))
        ic_lines = "\n".join(f"      {f}," for f in merged)
        new_list_text = f"ignore_changes = [\n{ic_lines}\n    ]"
        new_lc_body = (
            lc_body[:ignore_match.start()]
            + new_list_text
            + lc_body[ignore_match.end():]
        )
        # Rebuild full hcl: keep prefix up to lifecycle body, replace
        # body, keep suffix after lifecycle close.
        abs_lc_body_start = body_start + lc_body_start
        abs_lc_body_end = body_start + lc_body_end
        new_hcl = (
            hcl[:abs_lc_body_start]
            + new_lc_body
            + hcl[abs_lc_body_end:]
        )
        return new_hcl, len(new_fields_set)

    # Lifecycle block exists but has no ignore_changes. Add it
    # at the top of the lifecycle body.
    ic_lines = "\n".join(f"      {f}," for f in fields)
    new_ic_block = (
        f"\n    ignore_changes = [\n"
        f"{ic_lines}\n"
        f"    ]"
    )
    abs_lc_body_start = body_start + lc_body_start
    new_hcl = (
        hcl[:abs_lc_body_start]
        + new_ic_block
        + hcl[abs_lc_body_start:]
    )
    return new_hcl, len(fields)


def _delete_block_at_root(hcl: str, block_name: str) -> Tuple[str, int]:
    """Delete every top-level ``block_name { ... }`` block from a
    resource's body, brace-depth-aware.

    PUI-1F v3.4 (2026-04-29 cluster smoke): the existing
    ``_delete_at_top_level`` only handles ``field = value`` lines.
    This helper deletes whole nested blocks like:

        managed_prometheus_config {
          enabled = true
        }

    when the block name has NO valid placement in the schema (LLM
    hallucinated a v1-vestige or wrong-shape block name -- the
    actual schema has ``monitoring_config.managed_prometheus``,
    no top-level ``managed_prometheus_config``).

    Brace-depth-aware: descends ``{`` and ``}`` properly so a nested
    body with internal braces (e.g. inside a containers block of
    cloud_run) is not falsely matched. Skips anywhere ``block_name``
    appears as a sub-block (depth > 0) -- only deletes when the
    keyword is at the resource body's top level.

    Returns (new_hcl, blocks_deleted_count). Idempotent: running
    twice on the same input returns 0 the second time.

    GENUINE fix when the block is a hallucination (no valid
    placement). WORKAROUND if the block IS valid in some other
    location and we're just unable to relocate it -- in that case
    the block's contents are silently lost (mark with #CAVEAT in the
    JSON entry's comment).
    """
    # Find the outermost resource body.
    resource_match = re.search(
        r'^resource\s+"[^"]+"\s+"[^"]+"\s*\{',
        hcl,
        re.MULTILINE,
    )
    if not resource_match:
        return hcl, 0
    body_start = resource_match.end()
    body_depth_start = 1
    body_end = body_start
    while body_end < len(hcl) and body_depth_start > 0:
        c = hcl[body_end]
        if c == '{':
            body_depth_start += 1
        elif c == '}':
            body_depth_start -= 1
            if body_depth_start == 0:
                break
        body_end += 1
    if body_depth_start != 0:
        return hcl, 0  # malformed; bail safely

    # Walk the body, scanning for `block_name {` at body-relative depth 0.
    out_chunks: List[str] = [hcl[:body_start]]
    cursor = body_start
    body_depth = 0
    deletions = 0
    kw_len = len(block_name)
    i = body_start
    while i < body_end:
        if body_depth == 0:
            # Word-boundary on the left.
            left_ok = (
                i == body_start
                or (not hcl[i - 1].isalnum() and hcl[i - 1] != '_')
            )
            if left_ok and hcl[i:i + kw_len] == block_name:
                j = i + kw_len
                # Word-boundary on the right.
                if j < len(hcl) and (
                    hcl[j].isalnum() or hcl[j] == '_'
                ):
                    pass  # not a full match (e.g. "managed_prometheus_config_extra")
                else:
                    # Skip whitespace, expect '{'.
                    while j < body_end and hcl[j] in ' \t':
                        j += 1
                    if j < body_end and hcl[j] == '{':
                        # Found block opener. Find matching close.
                        block_depth = 1
                        k = j + 1
                        while k < body_end and block_depth > 0:
                            if hcl[k] == '{':
                                block_depth += 1
                            elif hcl[k] == '}':
                                block_depth -= 1
                                if block_depth == 0:
                                    break
                            k += 1
                        if block_depth == 0:
                            # Compute the line-start (back to last \n) so
                            # we eat indentation.
                            line_start = i
                            while (line_start > body_start
                                   and hcl[line_start - 1] in ' \t'):
                                line_start -= 1
                            # Eat the trailing newline after the closing }.
                            end_pos = k + 1
                            if end_pos < body_end and hcl[end_pos] == '\n':
                                end_pos += 1
                            # Emit the gap before this block, skip the block.
                            out_chunks.append(hcl[cursor:line_start])
                            cursor = end_pos
                            deletions += 1
                            i = end_pos
                            continue
        # Generic depth tracking.
        if hcl[i] == '{':
            body_depth += 1
        elif hcl[i] == '}':
            body_depth -= 1
        i += 1
    # Tail.
    out_chunks.append(hcl[cursor:])
    return ''.join(out_chunks), deletions


def _delete_block_in_text(text: str, block_name: str) -> Tuple[str, int]:
    """Helper: delete every ``block_name { ... }`` at depth 0 of `text`.

    Brace-depth-aware (descends nested ``{`` ``}`` correctly so a sub-
    block sharing the name doesn't get false-matched). Returns
    (new_text, blocks_deleted_count). Reused by both
    ``_delete_block_at_root`` (text = the resource body) and
    ``_delete_block_in_path`` (text = each parent-path range).
    """
    out_chunks: List[str] = []
    cursor = 0
    depth = 0
    deletions = 0
    kw_len = len(block_name)
    i = 0
    while i < len(text):
        if depth == 0:
            left_ok = (
                i == 0
                or (not text[i - 1].isalnum() and text[i - 1] != '_')
            )
            if left_ok and text[i:i + kw_len] == block_name:
                j = i + kw_len
                # Right word-boundary check.
                if j < len(text) and (
                    text[j].isalnum() or text[j] == '_'
                ):
                    pass
                else:
                    while j < len(text) and text[j] in ' \t':
                        j += 1
                    if j < len(text) and text[j] == '{':
                        block_depth = 1
                        k = j + 1
                        while k < len(text) and block_depth > 0:
                            if text[k] == '{':
                                block_depth += 1
                            elif text[k] == '}':
                                block_depth -= 1
                                if block_depth == 0:
                                    break
                            k += 1
                        if block_depth == 0:
                            line_start = i
                            while (line_start > 0
                                   and text[line_start - 1] in ' \t'):
                                line_start -= 1
                            end_pos = k + 1
                            if end_pos < len(text) and text[end_pos] == '\n':
                                end_pos += 1
                            out_chunks.append(text[cursor:line_start])
                            cursor = end_pos
                            deletions += 1
                            i = end_pos
                            continue
        if text[i] == '{':
            depth += 1
        elif text[i] == '}':
            depth -= 1
        i += 1
    out_chunks.append(text[cursor:])
    return ''.join(out_chunks), deletions


def _delete_block_in_path(
    hcl: str, block_path: str, block_name: str,
) -> Tuple[str, int]:
    """Delete every ``block_name { ... }`` block found INSIDE the
    nested ``block_path`` (e.g. ``node_pool_defaults.node_config_defaults``).

    PUI-1F v3.6 (2026-04-29 Standard cluster smoke): the existing
    ``_delete_block_at_root`` only operates at the resource body root.
    This helper handles the nested case -- LLM emits
    ``node_pool_defaults { node_config_defaults { logging_config {} } }``
    where ``logging_config`` has no valid placement at that depth in
    the provider schema (the provider uses a flat ``logging_variant``
    attribute there). Reuses ``_find_block_path_ranges`` to locate
    the parent path's ranges, then runs ``_delete_block_in_text`` on
    each range's interior.

    Iterates ranges in REVERSE so an earlier deletion's index shift
    doesn't invalidate later ranges.
    """
    ranges = _find_block_path_ranges(hcl, block_path)
    if not ranges:
        return hcl, 0
    total = 0
    for start, end in reversed(ranges):
        range_text = hcl[start:end]
        new_range_text, n = _delete_block_in_text(range_text, block_name)
        if n > 0:
            hcl = hcl[:start] + new_range_text + hcl[end:]
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

    # PUI-1F v3.4 (2026-04-29 cluster smoke): block_deletions for whole
    # ``block_name { ... }`` constructs (vs `deletions` which only
    # handles `field = value` lines). Use ONLY when the block name
    # has NO valid placement in the schema at the named depth.
    #
    # Each entry: {comment: str, block_name: str, block_path?: str}
    #   block_path missing/empty -> delete from resource body root
    #     (uses _delete_block_at_root)
    #   block_path = "parent.subparent" -> delete from inside that
    #     nested path only (uses _delete_block_in_path) -- v3.6
    #     extension for cases like
    #     `node_pool_defaults.node_config_defaults.logging_config`
    #     where the block name IS valid at the resource root (e.g.
    #     `logging_config` is a top-level block) but NOT inside the
    #     specified nested path. block_path scoping prevents the
    #     root-level valid block from being collateral damage.
    for block_del in rules.get("block_deletions", []):
        try:
            block_name = block_del["block_name"]
            block_path = block_del.get("block_path", "")
        except (KeyError, TypeError) as e:
            _log.warning(
                "post_llm_override_malformed",
                kind="block_deletions",
                tf_type=tf_type,
                missing_key=str(e),
            )
            continue
        if block_path:
            hcl_text, n = _delete_block_in_path(
                hcl_text, block_path, block_name,
            )
            scope_label = f"{block_path}."
        else:
            hcl_text, n = _delete_block_at_root(hcl_text, block_name)
            scope_label = "<root>."
        if n > 0:
            corrections.append(
                f"deleted block '{scope_label}{block_name}' ({n}x)"
            )
            _log.info(
                "post_llm_block_deleted",
                tf_type=tf_type,
                block_path=block_path or "<root>",
                block_name=block_name,
                count=n,
            )

    # PUI-1F v3.3 Fix 2: lifecycle.ignore_changes injection. Each entry
    # is {comment: str, fields: list[str]}. Multiple entries on the same
    # tf_type are merged additively into one lifecycle block (the
    # injector dedups). A single entry per tf_type is the common case.
    for inj in rules.get("lifecycle_ignore_changes", []):
        try:
            fields = inj["fields"]
            if not isinstance(fields, list) or not fields:
                raise ValueError(
                    f"'fields' must be a non-empty list, got {fields!r}",
                )
        except (KeyError, TypeError, ValueError) as e:
            _log.warning(
                "post_llm_override_malformed",
                kind="lifecycle_ignore_changes",
                tf_type=tf_type,
                missing_key=str(e),
            )
            continue
        hcl_text, n = _inject_lifecycle_ignore_changes(hcl_text, fields)
        if n > 0:
            corrections.append(
                f"injected lifecycle.ignore_changes "
                f"+= [{', '.join(fields)}] ({n} new field(s))"
            )
            _log.info(
                "post_llm_lifecycle_injected",
                tf_type=tf_type,
                fields=fields,
                fields_added=n,
            )

    return hcl_text, corrections


def reset_cache() -> None:
    """Clear the cached overrides. Intended for tests; not needed in
    normal use."""
    global _cached_overrides
    _cached_overrides = None
