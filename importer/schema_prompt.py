# importer/schema_prompt.py
"""
Schema-summary builder for the HCL-generation prompt (PR-5).

Lives in its own module — and deliberately depends only on standard library
types — so it can be unit-tested without dragging in `llm_provider` /
`vertexai` / langchain. `hcl_generator.py` imports `build_schema_summary`
from here.

The old prompt surfaced only the flat list of argument names:

    Valid arguments for `google_compute_instance` are: boot_disk, ...

That's enough context for simple resources but catastrophically thin for
anything with deep nesting — GKE clusters (716 paths), SQL instances (183
paths), compute instances (168 paths). Without telling the LLM which
blocks are required, which are singletons, which nested fields are
required inside a block, and which fields are read-only, the model has
to *guess* those constraints from prior training — and it gets them
wrong in ways that cost us heuristics.

The new summary is schema-derived from the `paths` index produced by
`build_kb.py` (which itself comes from `terraform providers schema -json`).
It is strictly more information than the old format, organized so the LLM
can scan and apply it.
"""

from typing import Any


def _type_str(type_spec: Any) -> str:
    """Render a schema type for humans. Types in the schema dump are either a
    bare string (`"string"`, `"number"`) or a nested list form
    (`["map", "string"]`, `["list", ["object", {...}]]`)."""
    if isinstance(type_spec, str):
        return type_spec
    if isinstance(type_spec, list) and type_spec:
        outer = type_spec[0]
        if len(type_spec) > 1:
            inner = type_spec[1]
            if isinstance(inner, (list, dict)):
                return f"{outer}(object)"
            return f"{outer} of {inner}"
        return str(outer)
    return "?"


def _block_cardinality(info: dict) -> str:
    """Human-readable cardinality string for a block path entry."""
    mn = info.get("min_items")
    mx = info.get("max_items")
    if mn == 1 and mx == 1:
        return "exactly 1"
    if mn in (None, 0) and mx == 1:
        return "0 or 1"
    if mn == 1 and mx is None:
        return "1+"
    if (mn in (None, 0)) and mx is None:
        return "0+"
    return f"{mn or 0}..{mx if mx is not None else 'inf'}"


def build_schema_summary(tf_type: str, schema: dict) -> str:
    """Compact, scannable schema summary for the LLM system prompt.

    Falls back to the old one-line format when the KB file is older and
    lacks a `paths` index.
    """
    if not schema:
        return ""

    paths = schema.get("paths") or {}
    if not paths:
        # Legacy KB file — degrade gracefully.
        args = schema.get("arguments") or []
        if not args:
            return ""
        names = ", ".join(a["name"] for a in args)
        return f"\n\nValid arguments for `{tf_type}` are: {names}."

    required_top: list = []
    optional_top_attrs: list = []
    optional_top_blocks: list = []
    required_nested: list = []
    computed_only: list = []

    for path in sorted(paths):
        info = paths[path]
        if info.get("deprecated"):
            continue
        kind = info.get("kind")
        is_top = "." not in path

        if kind == "attribute":
            t = _type_str(info.get("type"))
            if info.get("required"):
                if is_top:
                    required_top.append(f"{path} : {t}")
                else:
                    required_nested.append(f"{path} : {t}")
            elif info.get("computed") and not info.get("optional"):
                if is_top:
                    computed_only.append(path)
            elif info.get("optional") and is_top:
                optional_top_attrs.append(f"{path} : {t}")
        elif kind == "block":
            if not is_top:
                continue  # nested blocks surface through required_nested
            card = _block_cardinality(info)
            label = f"{path} (block, {card})"
            if info.get("required"):
                required_top.append(label)
            else:
                optional_top_blocks.append(label)

    lines = [f"\n\n=== SCHEMA FOR `{tf_type}` (authoritative) ==="]

    if required_top:
        lines.append("\nREQUIRED - must appear in HCL:")
        for r in required_top:
            lines.append(f"  * {r}")

    if required_nested:
        lines.append("\nREQUIRED inside their parent block:")
        for r in required_nested:
            lines.append(f"  * {r}")

    if optional_top_blocks:
        lines.append("\nOPTIONAL BLOCKS (use HCL block syntax, not `=` assignment):")
        for b in optional_top_blocks:
            lines.append(f"  - {b}")

    if optional_top_attrs:
        lines.append("\nOPTIONAL attributes (top-level):")
        # Cap listing length to keep the prompt scannable.
        for a in optional_top_attrs[:60]:
            lines.append(f"  - {a}")
        if len(optional_top_attrs) > 60:
            lines.append(f"  - ... and {len(optional_top_attrs) - 60} more")

    if computed_only:
        lines.append("\nDO NOT WRITE - these are read-only, provider-set:")
        lines.append("  " + ", ".join(computed_only))

    lines.append("\nRules:")
    lines.append("  * Block-typed fields use `name { ... }` syntax, NOT `name = [...]`.")
    lines.append("  * For list-nested blocks, repeat the block: "
                 "`network_interface { ... }  network_interface { ... }`.")
    lines.append("  * Every REQUIRED field above MUST appear in the HCL.")
    lines.append("  * Every DO-NOT-WRITE field MUST be ABSENT from the HCL. "
                 "Do NOT put DO-NOT-WRITE fields in `lifecycle.ignore_changes` either - "
                 "Terraform rejects ignoring pure-computed attributes.")
    lines.append("  * Optional location-like fields (`zone`, `region`, `location`, "
                 "`project`) are functionally required by the GCP provider's "
                 "Read path: if the input JSON contains a value, you MUST write it.")
    lines.append("  * ROUND-TRIP FIDELITY: every key present in the input JSON whose "
                 "name appears anywhere in this schema (top-level OR inside any block) "
                 "MUST appear in your HCL with the same value. Do not selectively "
                 "drop nested fields like `scheduling.instance_termination_action`, "
                 "`boot_disk.device_name`, `service_account.email`, etc. - the cloud "
                 "set them and `terraform plan` will show drift if they are missing.")
    lines.append("  * `lifecycle.ignore_changes` entries are HCL identifier references, "
                 "NOT strings. Write `ignore_changes = [zone, labels]` and never "
                 "`ignore_changes = [\"zone\", \"labels\"]` - the quoted form is "
                 "deprecated and emits a warning.")

    return "\n".join(lines)
