# importer/post_llm_validation.py
"""
Schema-aware post-LLM validation pass for generated HCL.

Catches LLM hallucinations that produce syntactically valid HCL but
semantically broken configs that fail at `terraform plan`. Distinct
from `post_llm_overrides.py` (which renames/deletes specific known
fields): this module is RULE-driven from the provider schema oracle,
not from a hardcoded override list.

Why this exists
---------------
The Phase 1 SMOKE against dev-proj-470211 surfaced a recurring LLM
hallucination class: when the cloud snapshot has a parent block whose
inner fields aren't in the snapshot (or aren't well-understood), the
LLM faithfully reflects "there's a block here" by emitting an empty
block:

    pod_cidr_overprovision_config {}
    client_certificate_config {}
    pubsub {}
    advanced_datapath_observability_config {}

But the provider rejects every one of them at plan time because the
schema requires inner fields that the LLM didn't include:

    Error: Missing required argument
    The argument "disabled" is required, but no definition was found.

The pre-existing `_attempt_correction` retry loop CAN fix these with
extra LLM calls -- but each fix risks introducing a new hallucination,
costs LLM tokens, and pads HITL load on the operator. A deterministic
post-LLM pass kills the bug class with zero LLM cost and zero
non-determinism.

Design
------
Schema-driven, not allowlist-driven. We use the schema oracle to ask
"does this block have ANY required inner field?" and act on that:

  * Block has at least one required inner field AND was emitted empty
    -> LLM hallucination, drop it. Plan would have failed anyway, so
       removing the block is strictly safer than leaving it.
  * Block has only optional inner fields AND was emitted empty
    -> Could be a legitimately presence-only signal (e.g.
       `master_auth {}` on a GKE cluster disables basic auth by its
       very presence). Keep it.

Fail-safe: any oracle error or schema miss leaves the HCL unchanged.
The downstream self-correction loop catches anything we miss.

Scope
-----
* Top-level block detection only. A nested empty block (e.g.
  `boot_disk { initialize_params {} }`) is technically possible to
  hallucinate, but the regex anchoring makes it ~impossible to detect
  reliably without a full HCL parser. Out of scope until evidence
  shows it matters.
* Single-line and multi-line empty blocks both detected (`name {}` and
  `name {\n}`).
* Block names matched as bare identifiers — no prefix/suffix matching,
  so a partial name like `inner_block` inside `outer_block { inner... }`
  doesn't false-match.
"""

from __future__ import annotations

import re
from typing import Any, List, Optional, Tuple

from common.logging import get_logger

from . import schema_oracle

_log = get_logger(__name__)


# Match a top-level (or any-indent) empty block on its own line(s):
#   `block_name {}`
#   `block_name { }`
#   `block_name {\n}`
#   `block_name {\n  \n}`  (whitespace-only body)
#
# Anchoring rationale:
#   ^[ \t]*       leading line indent (any depth)
#   ([A-Za-z_]\w*)  block name as a bare identifier (no `=` -> not an attribute)
#   \s*\{         opening brace after optional whitespace
#   \s*           any whitespace inside, including newlines (re's \s does match \n)
#   \}            closing brace
#   [ \t]*\n?     trailing same-line whitespace + optional newline
#                 (so the substitution removes the entire line cleanly)
_EMPTY_BLOCK_RE = re.compile(
    r"^[ \t]*([A-Za-z_]\w*)\s*\{\s*\}[ \t]*\n?",
    re.MULTILINE,
)


def find_empty_blocks(hcl_text: str) -> List[Tuple[str, int, int]]:
    """Locate every empty block in `hcl_text`.

    Returns a list of (block_name, match_start, match_end) tuples.
    Empty result means no empty blocks were found. Pure function; no
    schema lookups, suitable for unit testing without an oracle.
    """
    return [
        (m.group(1), m.start(), m.end())
        for m in _EMPTY_BLOCK_RE.finditer(hcl_text)
    ]


def _block_has_required_inner_field(
    oracle: Any, tf_type: str, block_name: str,
) -> bool:
    """Return True iff the schema for `tf_type` says block `block_name`
    has at least one required inner attribute.

    Walks the oracle's flat path index, looking for entries that match
    the prefix `<block_name>.` and have `required=True`. We check
    attribute paths only -- nested-block "required" is encoded via
    min_items >= 1, which is a different concern (a missing nested
    block at the parent level, not an empty block emitted at this
    level).

    Fail-safe: any oracle error returns False (don't drop). The downstream
    self-correction loop will surface anything we miss.
    """
    try:
        if not oracle.has(tf_type):
            return False
        prefix = f"{block_name}."
        for path in oracle.list_paths(tf_type, kind="attribute"):
            if not path.startswith(prefix):
                continue
            info = oracle.get(tf_type, path)
            if info is not None and info.required:
                return True
        return False
    except Exception:  # noqa: BLE001 - fail open: leave the block alone
        return False


def drop_required_field_empty_blocks(
    hcl_text: str,
    tf_type: str,
    *,
    oracle: Optional[Any] = None,
) -> Tuple[str, List[str]]:
    """Drop LLM-hallucinated empty blocks whose schema requires inner fields.

    Args:
        hcl_text: the LLM-generated HCL (already past
            post_llm_overrides.apply_overrides).
        tf_type: terraform resource type (e.g. "google_container_cluster").
        oracle: optional SchemaOracle injected for testing. Production
            callers omit; we lazy-load via schema_oracle.get_oracle().

    Returns:
        (cleaned_hcl, dropped_block_names)

        `dropped_block_names` is sorted and de-duplicated for log
        emission. Empty when nothing was dropped (the common case --
        well-formed HCL has no empty blocks).

    Fail-safe: oracle errors / unknown tf_types return the input
    unchanged with an empty list. The downstream self-correction loop
    catches anything we miss; we never want this layer to be the
    reason a workflow fails.
    """
    if not hcl_text:
        return hcl_text, []

    candidates = find_empty_blocks(hcl_text)
    if not candidates:
        return hcl_text, []

    if oracle is None:
        try:
            oracle = schema_oracle.get_oracle()
        except Exception as e:  # noqa: BLE001 - fail open
            _log.warning(
                "post_llm_validation_oracle_unavailable",
                tf_type=tf_type,
                error=str(e),
                fallback="returning_input_unchanged",
            )
            return hcl_text, []

    # Decide per-candidate: drop iff schema demands required inner fields.
    # Process matches in REVERSE offset order so earlier indices stay valid
    # as we mutate the string from the end -- same pattern as
    # post_llm_overrides._rename_in_block.
    dropped: List[str] = []
    for block_name, start, end in reversed(candidates):
        if _block_has_required_inner_field(oracle, tf_type, block_name):
            hcl_text = hcl_text[:start] + hcl_text[end:]
            dropped.append(block_name)

    return hcl_text, sorted(set(dropped))
