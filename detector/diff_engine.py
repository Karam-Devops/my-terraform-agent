# detector/diff_engine.py
"""
Pure-stdlib semantic diff between Terraform-state attributes (snake_case)
and live cloud JSON (camelCase).

Strategy:
  1. Normalize the cloud JSON: camelCase -> snake_case, strip URL prefixes,
     drop ignored fields.
  2. Normalize the state attributes: drop ignored fields, treat [] / "" / null
     as equivalent to "absent".
  3. Recursively walk both sides keyed by snake_case path.
  4. Emit a list of DriftItem records.

We do NOT try to be a perfect terraform-plan replacement — just to surface
real differences in human-meaningful fields. The truth-of-last-resort remains
`terraform plan`, which the executor will run after any remediation.
"""

import re
from dataclasses import dataclass, field
from typing import Any, List, Optional

from . import config


# --- Normalization helpers ---

_CAMEL_RE = re.compile(r"(?<!^)(?=[A-Z])")


def camel_to_snake(name: str) -> str:
    return _CAMEL_RE.sub("_", name).lower()


def _strip_url(value: Any) -> Any:
    """Strips known GCP self-link prefixes so URL-vs-shortname diffs vanish."""
    if not isinstance(value, str):
        return value
    for prefix in config.URL_PREFIXES_TO_STRIP:
        if value.startswith(prefix):
            return value[len(prefix):]
    return value


def _is_empty(value: Any) -> bool:
    """Treat [], {}, '', None as the same 'unset' value."""
    return value is None or value == [] or value == {} or value == ""


def _normalize_cloud(node: Any, ignored: set) -> Any:
    """Recursively normalize cloud JSON to look like Terraform state shape."""
    if isinstance(node, dict):
        out = {}
        for k, v in node.items():
            snake = camel_to_snake(k)
            if snake in ignored or k in ignored:
                continue
            normalized = _normalize_cloud(v, ignored)
            if _is_empty(normalized):
                continue
            out[snake] = normalized
        return out
    if isinstance(node, list):
        return [_normalize_cloud(x, ignored) for x in node]
    return _strip_url(node)


def _normalize_state(node: Any, ignored: set) -> Any:
    """Strip ignored keys and empty values from the state attribute tree."""
    if isinstance(node, dict):
        out = {}
        for k, v in node.items():
            if k in ignored:
                continue
            normalized = _normalize_state(v, ignored)
            if _is_empty(normalized):
                continue
            out[k] = normalized
        return out
    if isinstance(node, list):
        return [_normalize_state(x, ignored) for x in node]
    return _strip_url(node)


# --- Diff data model ---

@dataclass
class DriftItem:
    path: str            # dotted path, e.g. "boot_disk[0].auto_delete"
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


# --- Recursive diff walker ---

def _walk(state: Any, cloud: Any, path: str, out: List[DriftItem]) -> None:
    # Both empty -> nothing to do
    if _is_empty(state) and _is_empty(cloud):
        return

    # One side empty -> added or removed
    if _is_empty(state) and not _is_empty(cloud):
        out.append(DriftItem(path=path, op="added", cloud_value=cloud))
        return
    if _is_empty(cloud) and not _is_empty(state):
        out.append(DriftItem(path=path, op="removed", state_value=state))
        return

    # Type mismatch -> treat as changed
    if type(state) != type(cloud):
        out.append(DriftItem(
            path=path, op="changed",
            state_value=state, cloud_value=cloud,
        ))
        return

    if isinstance(state, dict):
        for key in sorted(set(state.keys()) | set(cloud.keys())):
            child_path = f"{path}.{key}" if path else key
            _walk(state.get(key), cloud.get(key), child_path, out)
        return

    if isinstance(state, list):
        # Length mismatch is a real diff
        if len(state) != len(cloud):
            out.append(DriftItem(
                path=path, op="changed",
                state_value=state, cloud_value=cloud,
            ))
            return
        for i, (s, c) in enumerate(zip(state, cloud)):
            _walk(s, c, f"{path}[{i}]", out)
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
    norm_state = _normalize_state(state_attrs, ignored)
    norm_cloud = _normalize_cloud(cloud_json, ignored)

    _walk(norm_state, norm_cloud, path="", out=drift.items)
    return drift


# --- Reporting ---

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