"""Load + merge customer translation profiles from YAML files.

Replaces the hardcoded `_SOURCE_REF_SUBSTITUTIONS` table in
terraform_emitter.py and `_GCP_TO_AWS_LOCAL_REFS` in terragrunt_emitter.py
with YAML profile files under `customer_profiles/`. The engine loads
`_default.yaml` plus an optional customer-named profile, merges them
(customer overrides default), and returns a substitution table the
emitters apply to translator output.

API:
    get_substitutions(customer_profile="default") -> List[Tuple[str, str]]
        Returns substitutions as a list of (source-ref, target-ref)
        pairs sorted by source-ref length descending (so longer keys
        check first — prevents prefix-match false positives like
        `var.env` matching inside `var.environment`).

    list_available_profiles() -> List[str]
        Returns the names of profile YAMLs in the directory (excluding
        the `_default` profile). Used by the UI to populate the
        customer-profile dropdown.
"""

from __future__ import annotations

import os
from functools import lru_cache
from typing import Dict, List, Tuple

# Module-level constant — directory holding profile YAMLs. Resolved
# relative to this file so it works regardless of cwd at engine-launch.
_PROFILES_DIR = os.path.join(os.path.dirname(__file__), "customer_profiles")


def _load_yaml(path: str) -> Dict:
    """Read a YAML file, return its top-level dict. Returns {} if the
    file doesn't exist or fails to parse — we never raise from this
    loader so a malformed profile doesn't crash the engine.

    yaml dependency: install pyyaml if not present. Adding to
    requirements.txt as part of this change.
    """
    if not os.path.isfile(path):
        return {}
    try:
        import yaml  # type: ignore
    except ImportError:
        # If pyyaml isn't installed, just degrade to empty profile.
        # The engine still works; customer just gets neutral defaults.
        return {}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001 — best-effort
        return {}


@lru_cache(maxsize=32)
def get_substitutions(customer_profile: str = "default") -> Tuple[Tuple[str, str], ...]:
    """Return the merged substitution list for the named customer profile.

    The result is sorted by source-ref length descending so the
    longest keys check first — required to prevent prefix-match bugs
    (e.g., `var.env` matching the start of `var.environment`).

    Result is a tuple-of-tuples (not list) so it's hashable and
    cacheable. Callers can iterate and unpack like a list.
    """
    profile_name = (customer_profile or "default").strip().lower()
    if profile_name in ("", "none"):
        profile_name = "default"

    # Always load _default.yaml first as the baseline.
    default_data = _load_yaml(os.path.join(_PROFILES_DIR, "_default.yaml"))
    merged_subs: Dict[str, str] = dict(default_data.get("local_substitutions") or {})

    # Layer the customer profile on top (if it's not the default itself).
    if profile_name != "default":
        customer_data = _load_yaml(os.path.join(_PROFILES_DIR, f"{profile_name}.yaml"))
        for k, v in (customer_data.get("local_substitutions") or {}).items():
            merged_subs[k] = v  # customer overrides default

    # Sort by source-ref length descending (longer keys check first).
    sorted_pairs = sorted(merged_subs.items(), key=lambda kv: -len(kv[0]))
    return tuple(sorted_pairs)


def list_available_profiles() -> List[str]:
    """Return names of customer profiles available for selection in the UI.

    Excludes `_default` (always applied, not a selectable choice).
    """
    if not os.path.isdir(_PROFILES_DIR):
        return ["default"]
    profiles = ["default"]
    for fname in sorted(os.listdir(_PROFILES_DIR)):
        if not fname.endswith(".yaml"):
            continue
        if fname.startswith("_"):
            continue   # _default.yaml and any other internal-prefix files
        profiles.append(fname[:-5])  # strip ".yaml"
    return profiles


def get_profile_metadata(customer_profile: str = "default") -> Dict:
    """Return the metadata block of a customer profile (for UI tooltips).

    Falls back to a minimal placeholder if the profile doesn't exist
    or has no metadata. The `display_name` field lets a profile
    override its UI label — useful for acronyms like "DH" that
    Python's str.title() would mangle ("Dh").
    """
    profile_name = (customer_profile or "default").strip().lower()
    if profile_name == "default":
        data = _load_yaml(os.path.join(_PROFILES_DIR, "_default.yaml"))
    else:
        data = _load_yaml(os.path.join(_PROFILES_DIR, f"{profile_name}.yaml"))
    meta = data.get("metadata") or {}
    return {
        "name":         str(meta.get("name", profile_name)),
        # display_name defaults to title-cased name when not specified
        "display_name": str(meta.get("display_name") or str(meta.get("name", profile_name)).title()),
        "description":  str(meta.get("description", "(no description)")),
        "applies_to":   str(meta.get("applies_to", "(unspecified)")),
    }
