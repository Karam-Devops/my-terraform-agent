"""Rule-driven translator engine — declarative YAML → Translation.

Loads rule files from `migrator/translate/rules/*.yaml` at engine
startup, validates them against the schema (see `rules/_schema.md`),
and exposes `translate_from_rules(resource, compliance_profile)` that
the dispatcher calls when a rule exists for a resource's tf_type.

When the dispatcher has both a Python translator AND a YAML rule for
the same tf_type, the YAML rule wins (rule-driven path is preferred
for maintainability). Python translators are the fallback.

Architecture decision: validation runs at MODULE IMPORT time so
malformed rules fail loudly at engine startup, not silently during
a customer's translation run. This trades a bit of cold-start time
for much better operator UX.
"""

from __future__ import annotations

import importlib
import logging
import os
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Callable, Dict, List, Optional

from migrator.results import DiscoveredResource

from .base import Translation


logger = logging.getLogger(__name__)


_RULES_DIR = os.path.join(os.path.dirname(__file__), "rules")


# ---- Built-in transforms (used by `inputs.X.transform` rule field) ----

def _t_lowercase(v: Any) -> Any:
    return str(v).lower() if v is not None else v

def _t_uppercase(v: Any) -> Any:
    return str(v).upper() if v is not None else v

def _t_int(v: Any) -> Any:
    try:
        return int(v) if v is not None and v != "" else v
    except (ValueError, TypeError):
        return v

def _t_bool(v: Any) -> Any:
    if isinstance(v, bool):
        return v
    if isinstance(v, str):
        return v.lower() in ("true", "yes", "on", "1")
    return bool(v) if v is not None else v

def _t_string(v: Any) -> Any:
    return str(v) if v is not None else v

def _t_dotted_to_underscored(v: Any) -> Any:
    return str(v).replace(".", "_") if v is not None else v

def _t_quote_string(v: Any) -> Any:
    """Wrap in double quotes for HCL literal context."""
    if v is None:
        return None
    return f'"{v}"'


_TRANSFORMS_FIXED: Dict[str, Callable[[Any], Any]] = {
    "lowercase":            _t_lowercase,
    "uppercase":            _t_uppercase,
    "int":                  _t_int,
    "bool":                 _t_bool,
    "string":               _t_string,
    "dotted_to_underscored": _t_dotted_to_underscored,
    "quote_string":         _t_quote_string,
}

# Parameterized transforms: `strip_prefix:foo` or `strip_suffix:bar`.
# Detected by name pattern; applied via the apply_transform() function.


def apply_transform(v: Any, transform_spec: str) -> Any:
    """Apply a named transform (possibly parameterized) to a value."""
    if v is None:
        return None
    if not transform_spec:
        return v
    if transform_spec in _TRANSFORMS_FIXED:
        return _TRANSFORMS_FIXED[transform_spec](v)
    # Parameterized transforms
    if transform_spec.startswith("strip_prefix:"):
        prefix = transform_spec.split(":", 1)[1]
        s = str(v)
        return s[len(prefix):] if s.startswith(prefix) else s
    if transform_spec.startswith("strip_suffix:"):
        suffix = transform_spec.split(":", 1)[1]
        s = str(v)
        return s[:-len(suffix)] if s.endswith(suffix) else s
    # Unknown transform — pass-through with a warning logged at load time
    return v


# ---- Rule dataclass ----

@dataclass
class Rule:
    """A loaded + validated rule file. Frozen after load."""
    source_type:  str
    target_type:  str
    service_name: str
    confidence:   str
    description:  str
    inputs:       Dict[str, Any]                  # raw dict from YAML
    compliance_defaults: Dict[str, Dict[str, Any]]
    python_override:     Optional[str]            # dotted module path or None
    rule_file_path:      str                      # for debug + error messages

    @property
    def has_python_override(self) -> bool:
        return self.python_override is not None and self.python_override.strip() != ""


# ---- Loader + validator ----

def _load_yaml(path: str) -> Optional[Dict]:
    try:
        import yaml  # type: ignore
    except ImportError:
        logger.warning("pyyaml not installed; rules engine disabled")
        return None
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return yaml.safe_load(fh)
    except Exception as e:  # noqa: BLE001
        logger.error("rules: failed to parse %s: %s", path, e)
        return None


def _validate_rule(rule_dict: Dict, file_path: str) -> Optional[Rule]:
    """Validate a parsed rule dict against the schema. Returns the
    Rule object on success; logs + returns None on validation failure.
    """
    fname = os.path.basename(file_path)
    if not isinstance(rule_dict, dict):
        logger.error("rules: %s top-level is not a dict", fname)
        return None

    # Required fields
    for required in ("source_type", "target_type", "service_name"):
        if not rule_dict.get(required):
            logger.error("rules: %s missing required field '%s'", fname, required)
            return None

    source_type = str(rule_dict["source_type"]).strip()
    target_type = str(rule_dict["target_type"]).strip()
    service_name = str(rule_dict["service_name"]).strip()

    # Filename consistency check
    expected_basename = f"{source_type}.yaml"
    if fname != expected_basename:
        logger.error("rules: %s — filename doesn't match source_type "
                     "(expected %s)", fname, expected_basename)
        return None

    # Validate confidence
    confidence = str(rule_dict.get("confidence", "HIGH")).upper()
    if confidence not in ("HIGH", "MEDIUM", "LOW"):
        logger.error("rules: %s — confidence '%s' not in (HIGH, MEDIUM, LOW); "
                     "defaulting to HIGH", fname, confidence)
        confidence = "HIGH"

    # Inputs must be a dict
    inputs = rule_dict.get("inputs") or {}
    if not isinstance(inputs, dict):
        logger.error("rules: %s — `inputs` must be a map; got %s", fname, type(inputs).__name__)
        return None

    # Validate each input spec
    for input_name, spec in inputs.items():
        # Schema-only fields (documented per-item shape; not consumed)
        # are prefixed with `__` per convention. Skip them.
        if input_name.startswith("__"):
            continue
        if isinstance(spec, str):
            continue   # shorthand: source key string
        if not isinstance(spec, dict):
            logger.error("rules: %s — inputs.%s must be a string or dict",
                         fname, input_name)
            return None

        # for_each (NEW) — iterates over a source map/list and produces
        # a nested output map.
        if "for_each" in spec:
            fe = spec["for_each"]
            if not isinstance(fe, dict):
                logger.error("rules: %s — inputs.%s.for_each must be a map",
                             fname, input_name)
                return None
            src_field = fe.get("source")
            if not src_field:
                logger.error("rules: %s — inputs.%s.for_each.source is required",
                             fname, input_name)
                return None
            # source can be a string (single key) OR a list of fallback keys.
            if not isinstance(src_field, (str, list)):
                logger.error(
                    "rules: %s — inputs.%s.for_each.source must be string or list of strings",
                    fname, input_name,
                )
                return None
            shape = str(fe.get("shape", "map")).lower()
            if shape not in ("map", "list"):
                logger.error("rules: %s — inputs.%s.for_each.shape must be 'map' or 'list'",
                             fname, input_name)
                return None
            item_inputs = fe.get("item_inputs")
            if not isinstance(item_inputs, dict):
                logger.error("rules: %s — inputs.%s.for_each.item_inputs must be a map",
                             fname, input_name)
                return None
            # Validate each item_input spec recursively. Nested for_each
            # IS supported (e.g., subnet's secondary_ip_ranges list inside
            # each subnet item).
            for ii_name, ii_spec in item_inputs.items():
                if isinstance(ii_spec, str):
                    continue
                if not isinstance(ii_spec, dict):
                    logger.error(
                        "rules: %s — inputs.%s.for_each.item_inputs.%s must be a string or dict",
                        fname, input_name, ii_name,
                    )
                    return None
            continue   # for_each replaces other extraction directives

        if "enum_map" in spec and not isinstance(spec["enum_map"], dict):
            logger.error("rules: %s — inputs.%s.enum_map must be a map",
                         fname, input_name)
            return None
        if "transform" in spec:
            t = str(spec["transform"])
            # Check if it's a known fixed transform OR a parameterized one
            is_known = (
                t in _TRANSFORMS_FIXED
                or t.startswith("strip_prefix:")
                or t.startswith("strip_suffix:")
            )
            if not is_known:
                logger.warning("rules: %s — inputs.%s.transform '%s' "
                               "is not a known transform; will pass-through",
                               fname, input_name, t)

    # compliance_defaults must be a dict-of-dicts when present
    compliance_defaults = rule_dict.get("compliance_defaults") or {}
    if not isinstance(compliance_defaults, dict):
        logger.error("rules: %s — `compliance_defaults` must be a map", fname)
        return None
    for profile_name, profile_defaults in compliance_defaults.items():
        if not isinstance(profile_defaults, dict):
            logger.error("rules: %s — compliance_defaults.%s must be a map",
                         fname, profile_name)
            return None

    # Python override: importability check
    python_override = rule_dict.get("python_override")
    if python_override:
        python_override = str(python_override).strip()
        try:
            importlib.import_module(python_override)
        except ImportError as e:
            logger.error("rules: %s — python_override '%s' is not importable: %s",
                         fname, python_override, e)
            return None

    return Rule(
        source_type=source_type,
        target_type=target_type,
        service_name=service_name,
        confidence=confidence,
        description=str(rule_dict.get("description", "")),
        inputs=inputs,
        compliance_defaults=compliance_defaults,
        python_override=python_override,
        rule_file_path=file_path,
    )


@lru_cache(maxsize=1)
def load_all_rules() -> Dict[str, Rule]:
    """Discover, parse, validate, and cache all rule files.

    Returns a dict keyed by source_type (== GCP tf_type). Result is
    cached for the engine's lifetime; rules don't reload at runtime.
    """
    rules: Dict[str, Rule] = {}
    if not os.path.isdir(_RULES_DIR):
        return rules

    for fname in sorted(os.listdir(_RULES_DIR)):
        if not fname.endswith(".yaml"):
            continue
        if fname.startswith("_"):
            continue   # _schema.md and any other internal files
        full = os.path.join(_RULES_DIR, fname)
        parsed = _load_yaml(full)
        if parsed is None:
            continue
        rule = _validate_rule(parsed, full)
        if rule is None:
            continue
        if rule.source_type in rules:
            logger.error("rules: duplicate source_type '%s' in %s; "
                         "first definition wins", rule.source_type, fname)
            continue
        rules[rule.source_type] = rule

    if rules:
        logger.info("rules_engine: loaded %d rule file(s): %s",
                    len(rules), ", ".join(sorted(rules.keys())))
    return rules


# ---- Renderer ----

def _extract_input_value(spec: Any, source_args: Dict, key_name: str) -> Any:
    """Extract one input value from source_args per the rule's `inputs.X` spec.

    Returns None when the source is missing AND no default is set.
    Handles three forms:
      1. shorthand string (copy source key verbatim)
      2. dict with from/enum_map/transform/default
      3. dict with `for_each` (iterate over a source map/list, building
         a nested output map per item_inputs)
    """
    if isinstance(spec, str):
        # Shorthand: copy source key verbatim
        return source_args.get(spec)

    if not isinstance(spec, dict):
        return None

    # for_each iteration (NEW) — overrides the scalar extraction path.
    if "for_each" in spec:
        return _extract_for_each(spec["for_each"], source_args)

    # `from:` accepts either a single key OR a list of fallback keys.
    # List form is "try each in order; first non-empty wins" — same
    # pattern as for_each.source. Common when source uses different
    # attribute names across customer module libraries (e.g.
    # `dns_name` vs scalar `domain`).
    from_field = spec.get("from", key_name)
    if isinstance(from_field, list):
        value = None
        for k in from_field:
            v = source_args.get(str(k))
            if v is not None and v != "":
                value = v
                break
    else:
        value = source_args.get(from_field)

    # Apply enum_map (if value matches a key in the map)
    enum_map = spec.get("enum_map") or {}
    if value is not None and value in enum_map:
        value = enum_map[value]

    # Apply transform
    transform = spec.get("transform")
    if transform:
        value = apply_transform(value, str(transform))

    # Apply default if still empty
    if value is None or value == "":
        value = spec.get("default")

    return value


def _extract_for_each(fe_spec: Dict, source_args: Dict):
    """Build a nested output (map or list) by iterating over a source
    map or list and applying per-item attribute extraction.

    fe_spec shape:
        source:        <key> | [<key1>, <key2>, ...]
                                       — required; string for one key, OR
                                         list of fallback keys (first
                                         non-None wins, common pattern in
                                         existing translators)
        shape:         "map" | "list"   — INPUT shape (default "map")
        output_shape:  "map" | "list"   — OUTPUT shape (default "map")
        item_inputs:   <map>            — per-item attribute spec (recursive,
                                          nested for_each supported)
        synthesize_when_empty:          — OPTIONAL fallback that builds a
                                          ONE-entry map from scalar source
                                          fields when none of the source
                                          keys yielded a populated collection.
                                          Used for customer patterns like
                                          DH's cloud-dns where each stack has
                                          a single zone declared as scalars
                                          (domain = "...", managed_zone = "...")
                                          rather than a `zones = {...}` map.
                                          Shape:
                                            { key_from: <source-key-of-map-key> }
                                          item_inputs is reused — but applied
                                          to source_args (top level) instead
                                          of per-item dicts.

    Returns:
        dict keyed by item-key when output_shape="map" (preserves map
        keys from source, or synthesizes from name/id/index for lists)
        list of dicts when output_shape="list" (preserves source order;
        item_key is dropped, only the per-item attrs are emitted)
    """
    source_field = fe_spec.get("source")
    shape = str(fe_spec.get("shape", "map")).lower()
    output_shape = str(fe_spec.get("output_shape", "map")).lower()
    item_inputs = fe_spec.get("item_inputs") or {}
    synth = fe_spec.get("synthesize_when_empty") or None

    # Resolve source — try each fallback key in order until non-None.
    raw = None
    if isinstance(source_field, str):
        raw = source_args.get(source_field)
    elif isinstance(source_field, list):
        for k in source_field:
            v = source_args.get(str(k))
            if v is not None and v != {} and v != []:
                raw = v
                break
    if raw is None:
        # Last-resort: synthesize a single-entry map from top-level
        # scalar fields. The synth.key_from scalar in source_args
        # becomes the map key; item_inputs is applied against the
        # full source_args dict (not a per-item one) so its `from:`
        # references resolve to the top-level scalars.
        if synth and output_shape != "list":
            key_from = synth.get("key_from")
            if key_from:
                map_key = source_args.get(key_from)
                if map_key:
                    # Re-use the standard item builder but pass source_args
                    # as the "item" dict. This lets the same item_inputs
                    # spec serve both the iterating path and the synth path.
                    synth_item: Dict[str, Any] = {}
                    for ii_name, ii_spec in item_inputs.items():
                        if isinstance(ii_spec, dict) and ii_spec.get("default_from_key") is True:
                            mod_spec = {k: v for k, v in ii_spec.items()
                                        if k != "default_from_key"}
                            v = _extract_input_value(mod_spec, source_args, ii_name)
                            if v is None or v == "":
                                v = map_key
                            if v is not None:
                                synth_item[ii_name] = v
                            continue
                        v = _extract_input_value(ii_spec, source_args, ii_name)
                        if v is not None:
                            synth_item[ii_name] = v
                    safe_key = (
                        str(map_key)
                        .replace("-", "_")
                        .replace(".", "_")
                        .replace("/", "_")
                    )
                    return {safe_key: synth_item}
        return [] if output_shape == "list" else {}

    # Normalize source → list of (key, item_dict) tuples.
    # Three source shapes are common in customer Terragrunt repos:
    #   1. dict-of-dicts   {"name1": {attrs}, "name2": {attrs}}
    #   2. list-of-dicts   [{"name": "n1", ...}, {"name": "n2", ...}]
    #   3. list-of-strings ["n1", "n2", "n3"]   ← ECR's "repositories" pattern
    items: List[tuple] = []
    if shape == "map" and isinstance(raw, dict):
        for k, v in raw.items():
            if isinstance(v, dict):
                items.append((str(k), v))
            elif isinstance(v, str):
                # dict value is a bare string (rare; treat as {name: v})
                items.append((str(k), {"name": v}))
    elif shape == "list" and isinstance(raw, list):
        for i, v in enumerate(raw):
            if isinstance(v, dict):
                # Prefer explicit name field; fall back to indexed key
                key = str(v.get("name") or v.get("id") or f"item{i}")
                items.append((key, v))
            elif isinstance(v, str):
                # List-of-strings: each entry is just the item's name.
                # Synthesize a minimal item dict so item_inputs's `from: name`
                # spec finds the value.
                items.append((v, {"name": v}))
    elif shape == "map" and isinstance(raw, list):
        # Some source repos use a list where each entry has a 'name'
        # field that becomes the map key. Accept this graceful form.
        # Also handle bare strings (list-of-strings as map source).
        for i, v in enumerate(raw):
            if isinstance(v, dict):
                key = str(v.get("name") or v.get("id") or f"item{i}")
                items.append((key, v))
            elif isinstance(v, str):
                items.append((v, {"name": v}))

    # For each item, build the output per item_inputs.
    map_out: Dict[str, Dict] = {}
    list_out: List[Dict] = []
    for item_key, item_args in items:
        item_out: Dict[str, Any] = {}
        for ii_name, ii_spec in item_inputs.items():
            # Special directive: default_from_key (use the source map key
            # as the value when source field is missing). Common for
            # name fields where the dict key IS the resource name.
            if isinstance(ii_spec, dict) and ii_spec.get("default_from_key") is True:
                # Build a modified spec without default_from_key to
                # avoid recursion confusion, then extract with item-key
                # as a fallback default.
                mod_spec = {k: v for k, v in ii_spec.items() if k != "default_from_key"}
                value = _extract_input_value(mod_spec, item_args, ii_name)
                if value is None or value == "":
                    value = item_key
                if value is not None:
                    item_out[ii_name] = value
                continue

            value = _extract_input_value(ii_spec, item_args, ii_name)
            if value is not None:
                item_out[ii_name] = value

        if output_shape == "list":
            list_out.append(item_out)
        else:
            # Sanitize the item_key for HCL identifier safety (hyphens →
            # underscores, etc.). Same approach the existing Python
            # translators use.
            safe_key = (
                str(item_key)
                .replace("-", "_")
                .replace(".", "_")
                .replace("/", "_")
            )
            map_out[safe_key] = item_out

    return list_out if output_shape == "list" else map_out


def _render_hcl_value(v: Any, indent: int = 0) -> str:
    """Render a Python value as an HCL literal for embedding in inputs.

    Multi-line dicts when any nested value is itself a dict (preserves
    readability of for_each output). Single-line otherwise.
    """
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    if v is None:
        return "null"
    if isinstance(v, str):
        s = v
        # Pre-quoted (from quote_string transform): emit as-is
        if s.startswith('"') and s.endswith('"'):
            return s
        # Interpolation expression: emit as-is (wrapped in HCL string)
        if s.startswith("${") and s.endswith("}"):
            return f'"{s}"'
        # Plain string: quote + escape
        escaped = s.replace('\\', '\\\\').replace('"', '\\"')
        return f'"{escaped}"'
    if isinstance(v, list):
        if not v:
            return "[]"
        return "[" + ", ".join(_render_hcl_value(x) for x in v) + "]"
    if isinstance(v, dict):
        if not v:
            return "{}"
        # Multi-line if any nested value is a dict or list (typical for
        # for_each output). Single-line for flat scalar maps.
        has_nested = any(isinstance(x, (dict, list)) for x in v.values())
        pad = "  " * (indent + 1)
        close_pad = "  " * indent
        if has_nested:
            lines = ["{"]
            for k, val in v.items():
                key_str = _hcl_key(k)
                rendered = _render_hcl_value(val, indent=indent + 1)
                lines.append(f"{pad}{key_str} = {rendered}")
            lines.append(f"{close_pad}}}")
            return "\n".join(lines)
        # Flat scalar map → single-line
        parts = []
        for k, val in v.items():
            key_str = _hcl_key(k)
            parts.append(f"{key_str} = {_render_hcl_value(val)}")
        return "{ " + ", ".join(parts) + " }"
    return f'"{str(v)}"'


def _hcl_key(k: Any) -> str:
    """Format a dict key for HCL. Quotes when key has non-identifier chars."""
    s = str(k)
    if s.replace("_", "").replace("-", "").isalnum() and not s[0].isdigit() and "-" not in s:
        return s
    return f'"{s}"'


def translate_from_rule(
    resource: DiscoveredResource,
    rule: Rule,
    *,
    compliance_profile: str = "none",
) -> Translation:
    """Generate a Translation from a parsed rule + source resource args.

    If the rule has a python_override, dispatch to it. Otherwise apply
    the declarative path: per-input extract → compliance defaults →
    HCL render.
    """
    # Python escape hatch — load + call the override module
    if rule.has_python_override:
        try:
            mod = importlib.import_module(rule.python_override)
        except ImportError as e:
            logger.error("rules: failed to import python_override '%s' for %s: %s",
                         rule.python_override, rule.source_type, e)
            return Translation(
                service_name=rule.service_name,
                aws_inputs_hcl=f"# rule python_override import failed: {e}\n",
                notes=[f"python_override-import-error: {e}"],
            )
        if not hasattr(mod, "translate"):
            return Translation(
                service_name=rule.service_name,
                aws_inputs_hcl="# python_override missing translate() function\n",
                notes=["python_override-missing-translate"],
            )
        # Try the richest signature first, fall back to simpler ones.
        # This lets legacy Python translators (translate(resource) only)
        # serve as python_override targets without modification.
        rule_dict = {
            "inputs": rule.inputs,
            "compliance_defaults": rule.compliance_defaults,
        }
        for kwargs in (
            {"compliance_profile": compliance_profile, "rule_dict": rule_dict},
            {"compliance_profile": compliance_profile},
            {},
        ):
            try:
                return mod.translate(resource, **kwargs)
            except TypeError:
                continue
        # Last-resort: legacy positional-only signature
        return mod.translate(resource)

    # Declarative path
    source_args = resource.arguments or {}
    notes: List[str] = []

    # 1. Extract inputs per rule
    rendered_inputs: Dict[str, Any] = {}
    for input_name, spec in rule.inputs.items():
        value = _extract_input_value(spec, source_args, input_name)
        if value is not None:
            rendered_inputs[input_name] = value

    # 2. Apply compliance profile defaults (only fill GAPS — source wins)
    profile = (compliance_profile or "none").lower()
    profile_defaults = rule.compliance_defaults.get(profile) or {}
    profile_attrs_applied: List[str] = []
    for attr, default_val in profile_defaults.items():
        if attr not in rendered_inputs:
            rendered_inputs[attr] = default_val
            profile_attrs_applied.append(attr)

    if profile_attrs_applied:
        notes.append(
            f"compliance profile '{profile.upper()}' applied — "
            f"defaults forced on: {', '.join(profile_attrs_applied)}"
        )

    # 3. Render to HCL
    lines: List[str] = []
    lines.append(f"  # Translated from GCP {rule.source_type} via rule "
                 f"`migrator/translate/rules/{rule.source_type}.yaml`.")
    for k, v in rendered_inputs.items():
        rendered = _render_hcl_value(v)
        # Add per-line "compliance profile" comment for clarity
        comment = "   # compliance profile" if k in profile_attrs_applied else ""
        lines.append(f"  {k} = {rendered}{comment}")

    return Translation(
        service_name=rule.service_name,
        aws_inputs_hcl="\n".join(lines) + "\n",
        notes=notes,
    )


# ---- Public API used by the dispatcher ----

def get_rule_for_type(tf_type: str) -> Optional[Rule]:
    """Return the loaded Rule for a GCP tf_type, or None if no rule."""
    return load_all_rules().get(tf_type)


def list_rule_driven_types() -> List[str]:
    """Names of GCP tf_types currently covered by rule files. Used by
    publish_mapping_table to show '✅ rule-driven' status."""
    return sorted(load_all_rules().keys())
