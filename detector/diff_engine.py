# detector/diff_engine.py
"""
Pure-stdlib semantic diff between Terraform-state attributes (snake_case)
and live cloud JSON (camelCase).

Strategy:
  1. Normalize the cloud JSON: camelCase -> snake_case, apply provider-style
     field aliases, strip URL prefixes (full and `projects/.../<leaf>`),
     flatten the metadata.items shape, drop ignored fields.
  2. Normalize the state attributes: drop ignored fields, treat [] / "" / null
     as equivalent to "absent".
  3. Recursively walk both sides keyed by snake_case path. Inline-unwrap
     Terraform's [{...}] single-block encoding when the cloud side is a {...}.
  4. Emit a list of DriftItem records.

Truth-of-last-resort remains `terraform plan`, run by the executor after
any remediation. This engine is for fast detection, not formal proof.
"""

import fnmatch
import re
from dataclasses import dataclass, field
from typing import Any, List, Optional

from . import config


# --- Normalization helpers ------------------------------------------------

# Two-pass regex handles acronyms correctly:
#   networkIP        -> network_ip
#   networkInterfaces -> network_interfaces
#   IPAddress        -> ip_address
_CAMEL_ACRONYM_RE = re.compile(r"(.)([A-Z][a-z]+)")
_CAMEL_TAIL_RE = re.compile(r"([a-z0-9])([A-Z])")


def camel_to_snake(name: str) -> str:
    s1 = _CAMEL_ACRONYM_RE.sub(r"\1_\2", name)
    return _CAMEL_TAIL_RE.sub(r"\1_\2", s1).lower()


def _strip_url(value: Any) -> Any:
    """Strips known full-URL GCP self-link prefixes."""
    if not isinstance(value, str):
        return value
    for prefix in config.URL_PREFIXES_TO_STRIP:
        if value.startswith(prefix):
            return value[len(prefix):]
    return value


def _strip_to_leaf(value: Any) -> Any:
    """`projects/{p}/zones/{z}/machineTypes/X` -> `X`. Only safe for fields
    where the state side stores the bare leaf."""
    if isinstance(value, str) and value.startswith("projects/") and "/" in value:
        return value.rsplit("/", 1)[-1]
    return value


def _is_empty(value: Any) -> bool:
    """Treat [], {}, '', None as the same 'unset' value."""
    return value is None or value == [] or value == {} or value == ""


def _numeric_string_equals_int(a: Any, b: Any) -> bool:
    """
    True if one side is an int and the other is a string-encoded int with the
    same value. Used to absorb GCP's int64-as-JSON-string quirk
    (e.g., retention_duration_seconds = "604800" vs 604800 in state).
    Booleans are excluded — Python's `bool` is a subclass of `int`, and we
    don't want True/False being silently equated to "1"/"0".
    """
    def _coerce(v: Any) -> Optional[int]:
        if isinstance(v, bool):
            return None
        if isinstance(v, int):
            return v
        if isinstance(v, str):
            try:
                return int(v)
            except ValueError:
                return None
        return None

    ca, cb = _coerce(a), _coerce(b)
    return ca is not None and cb is not None and ca == cb


def _flatten_metadata_items(metadata_dict: Any) -> Any:
    """
    Cloud:  metadata = {items: [{key, value}, ...], ...}
    State:  metadata = {key1: val1, key2: val2, ...}
    Convert cloud form to state form. No-op if input doesn't match the shape.
    """
    if not isinstance(metadata_dict, dict):
        return metadata_dict
    items = metadata_dict.get("items")
    if not isinstance(items, list):
        return metadata_dict
    flattened: dict = {}
    for kv in items:
        if isinstance(kv, dict) and "key" in kv:
            flattened[kv["key"]] = kv.get("value")
    # Preserve any other (non-items) keys that survived the ignore pass.
    for k, v in metadata_dict.items():
        if k != "items" and k not in flattened:
            flattened[k] = v
    return flattened


def _filter_label_keys(labels_dict: Any, patterns: List[str]) -> Any:
    """
    Drop keys from a `labels` dict whose name matches any fnmatch glob in
    `patterns`. No-op when input isn't a dict or no patterns are configured.

    This exists to silence cloud-managed labels (`goog-ops-agent-policy`,
    `goog-terraform-provisioned`, etc.) that no human declared and that
    would otherwise show as drift forever. We deliberately filter BOTH
    sides — after a remediation Accept the cloud value lands in state too,
    so a state-side filter prevents the noise from leaking back in.
    Human-added keys (`team`, `env`, ...) are unaffected by `goog-*`.
    """
    if not patterns or not isinstance(labels_dict, dict):
        return labels_dict
    return {
        k: v for k, v in labels_dict.items()
        if not any(fnmatch.fnmatchcase(k, p) for p in patterns)
    }


# --- Normalizers ---------------------------------------------------------

def _normalize_cloud(node: Any, ignored: set, aliases: dict, leaf_only: set,
                     label_key_ignore: List[str]) -> Any:
    """Recursively reshape cloud JSON to look like Terraform state."""
    if isinstance(node, dict):
        out: dict = {}
        for k, v in node.items():
            snake = camel_to_snake(k)
            renamed = aliases.get(snake, snake)
            # Honour ignore in any of: original key, snake_case, renamed alias.
            if k in ignored or snake in ignored or renamed in ignored:
                continue
            normalized = _normalize_cloud(v, ignored, aliases, leaf_only, label_key_ignore)
            # Special-case: flatten metadata.items into a key-value map.
            if renamed == "metadata":
                normalized = _flatten_metadata_items(normalized)
            # Drop cloud-managed label keys (e.g. goog-*) before they reach
            # the diff walker. Same per-field treatment as metadata.items.
            if renamed == "labels":
                normalized = _filter_label_keys(normalized, label_key_ignore)
            # Leaf-only stripping for known projects/.../<leaf> fields.
            if renamed in leaf_only:
                normalized = _strip_to_leaf(normalized)
            if _is_empty(normalized):
                continue
            out[renamed] = normalized
        return out
    if isinstance(node, list):
        return [_normalize_cloud(x, ignored, aliases, leaf_only, label_key_ignore) for x in node]
    return _strip_url(node)


def _normalize_state(node: Any, ignored: set, label_key_ignore: List[str]) -> Any:
    """Strip ignored keys and empty values from the state attribute tree."""
    if isinstance(node, dict):
        out: dict = {}
        for k, v in node.items():
            if k in ignored:
                continue
            normalized = _normalize_state(v, ignored, label_key_ignore)
            # Symmetric treatment: drop cloud-managed label keys on the
            # state side too. Mostly a no-op (importer already strips
            # labels), but matters after an Accept that pulls cloud labels
            # into state, or for any externally-imported state.
            if k == "labels":
                normalized = _filter_label_keys(normalized, label_key_ignore)
            if _is_empty(normalized):
                continue
            out[k] = normalized
        return out
    if isinstance(node, list):
        return [_normalize_state(x, ignored, label_key_ignore) for x in node]
    return _strip_url(node)


# --- Diff data model -----------------------------------------------------

@dataclass
class DriftItem:
    path: str            # dotted path, e.g. "scheduling.preemptible"
    op: str              # "added" | "removed" | "changed"
    state_value: Any = None
    cloud_value: Any = None


@dataclass
class ResourceDrift:
    tf_address: str
    tf_type: str
    items: List[DriftItem] = field(default_factory=list)
    error: Optional[str] = None  # populated if cloud snapshot was missing

    @property
    def has_drift(self) -> bool:
        return bool(self.items) or self.error is not None


# --- Recursive diff walker -----------------------------------------------

_LIST_INDEX_RE = re.compile(r"\[\d+\]")


def _canonical_path(path: str) -> str:
    """Strip list indices so 'a[0].b[3].c' matches the rule 'a.b.c'."""
    return _LIST_INDEX_RE.sub("", path)


def _walk(state: Any, cloud: Any, path: str, out: List[DriftItem],
          path_ignore: set) -> None:
    # Block-shape impedance match: TF wraps single nested blocks as [{...}];
    # GCP returns them as {...}. Unwrap so we compare like-for-like.
    if (isinstance(state, list) and len(state) == 1
            and isinstance(state[0], dict) and isinstance(cloud, dict)):
        state = state[0]
    if (isinstance(cloud, list) and len(cloud) == 1
            and isinstance(cloud[0], dict) and isinstance(state, dict)):
        cloud = cloud[0]

    # Both empty -> nothing to do
    if _is_empty(state) and _is_empty(cloud):
        return

    # State default-zero rule: TF writes `0` to state for many unset numeric
    # fields; GCP simply omits them. Treat as in-sync. Asymmetric: we do NOT
    # silence cloud-side `0` against missing state — that could be real drift.
    if (isinstance(state, (int, float)) and state == 0
            and not isinstance(state, bool) and _is_empty(cloud)):
        return

    # State default-false rule: TF writes `false` to state for many unset
    # boolean fields (e.g., requester_pays, enable_object_retention,
    # default_event_based_hold); GCP simply omits them. Same asymmetry as
    # the 0-rule: state=true vs cloud-omitted is still real drift, only
    # state=false vs cloud-omitted is suppressed.
    if (isinstance(state, bool) and state is False and _is_empty(cloud)):
        return

    # One side empty -> added or removed
    if _is_empty(state) and not _is_empty(cloud):
        out.append(DriftItem(path=path, op="added", cloud_value=cloud))
        return
    if _is_empty(cloud) and not _is_empty(state):
        out.append(DriftItem(path=path, op="removed", state_value=state))
        return

    # Type mismatch -> treat as changed (with one tolerated exception below).
    if type(state) != type(cloud):
        # API-quirk tolerance: GCP returns int64 fields as JSON strings
        # (e.g., soft_delete_policy.retention_duration_seconds = "604800"),
        # while the TF state stores them as native ints. Treat ("604800", 604800)
        # as equal. Booleans are excluded because Python's bool is a subclass
        # of int and we don't want True == "1" or "true" coercions.
        if _numeric_string_equals_int(state, cloud):
            return
        out.append(DriftItem(
            path=path, op="changed",
            state_value=state, cloud_value=cloud,
        ))
        return

    if isinstance(state, dict):
        for key in sorted(set(state.keys()) | set(cloud.keys())):
            child_path = f"{path}.{key}" if path else key
            if _canonical_path(child_path) in path_ignore:
                continue
            _walk(state.get(key), cloud.get(key), child_path, out, path_ignore)
        return

    if isinstance(state, list):
        if len(state) != len(cloud):
            out.append(DriftItem(
                path=path, op="changed",
                state_value=state, cloud_value=cloud,
            ))
            return
        for i, (s, c) in enumerate(zip(state, cloud)):
            _walk(s, c, f"{path}[{i}]", out, path_ignore)
        return

    # Scalar comparison
    if state != cloud:
        out.append(DriftItem(
            path=path, op="changed",
            state_value=state, cloud_value=cloud,
        ))


def diff_resource(tf_address: str, tf_type: str,
                  state_attrs: dict, cloud_json: Optional[dict]) -> ResourceDrift:
    """Top-level entry: produces a ResourceDrift for one resource."""
    drift = ResourceDrift(tf_address=tf_address, tf_type=tf_type)

    if cloud_json is None:
        drift.error = "cloud snapshot unavailable (resource may have been deleted)"
        return drift

    ignored = config.fields_to_ignore_for(tf_type)
    aliases = config.aliases_for(tf_type)
    leaf_only = config.leaf_only_fields_for(tf_type)
    path_ignore = config.path_ignore_for(tf_type)
    label_key_ignore = config.label_key_ignore_for(tf_type)

    norm_state = _normalize_state(state_attrs, ignored, label_key_ignore)
    norm_cloud = _normalize_cloud(cloud_json, ignored, aliases, leaf_only, label_key_ignore)

    _walk(norm_state, norm_cloud, path="", out=drift.items, path_ignore=path_ignore)
    return drift


# --- Reporting -----------------------------------------------------------

def print_report(drifts: List[ResourceDrift]) -> int:
    """Pretty-prints the drift report. Returns the count of drifted resources."""
    drifted = [d for d in drifts if d.has_drift]
    clean = [d for d in drifts if not d.has_drift]

    print("\n" + "=" * 70)
    print("DRIFT REPORT")
    print("=" * 70)

    for d in clean:
        print(f"✅ {d.tf_address}  — in sync")

    for d in drifted:
        print(f"\n🛑 {d.tf_address}")
        if d.error:
            print(f"   ERROR: {d.error}")
            continue
        for item in d.items:
            if item.op == "added":
                print(f"   + {item.path}  (cloud-only)")
                print(f"       cloud: {_truncate(item.cloud_value)}")
            elif item.op == "removed":
                print(f"   - {item.path}  (state-only)")
                print(f"       state: {_truncate(item.state_value)}")
            else:
                print(f"   ~ {item.path}")
                print(f"       state: {_truncate(item.state_value)}")
                print(f"       cloud: {_truncate(item.cloud_value)}")

    print("\n" + "-" * 70)
    print(f"Summary: {len(clean)} in sync, {len(drifted)} drifted")
    return len(drifted)


def _truncate(value: Any, limit: int = 120) -> str:
    s = repr(value)
    return s if len(s) <= limit else s[:limit] + "..."
