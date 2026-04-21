# importer/lifecycle_planner.py
"""
Auto-derive `lifecycle.ignore_changes` entries from the schema oracle.

The problem this solves
-----------------------
Many Terraform attributes are flagged `optional + computed` in the provider
schema. Semantically that means "the user MAY set this; if they don't, the
provider will". Examples on `google_compute_instance`:

    guest_os_features        (inside boot_disk)
    key_revocation_action_type
    network_interface.access_config.name
    network_interface.access_config.type
    project, zone           (often filled from environment)

When `terraform import` runs, the cloud's value lands in state. If the LLM
faithfully writes that value into HCL, future `terraform plan` runs are
clean — until the provider decides to recompute the value, at which point
the user gets a perpetual diff. If the LLM omits the value, the same thing
happens immediately on the first plan.

The robust play is `lifecycle.ignore_changes`: capture the value in state at
import time, then suppress diffs on it forever. This is what experienced
operators do by hand. We can derive it directly from the schema instead of
asking humans to write per-resource heuristics.

Scope of this module
--------------------
* Top-level attributes only. Nested ignore_changes (e.g.
  `boot_disk[0].guest_os_features`) is technically supported by Terraform
  but the syntax is finicky and a wrong entry breaks the plan; we keep it
  out of scope until there's evidence we need it.
* Only attributes that ARE `optional+computed` AND have a non-empty value
  in the cloud snapshot — i.e. the provider actually returned something.
  Attributes the cloud left empty don't need ignoring; they have no value
  to diff against.
* Skips required and pure-computed fields (those are handled by the LLM
  prompt and PR-3's auto-scrub respectively).
"""

from typing import Any, List, Set

from . import schema_oracle


# Framework meta-attributes that must NEVER appear in `lifecycle.ignore_changes`,
# even if the schema says they're `optional+computed`. Most provider schemas
# mark `id` as optional+computed (it's queryable), but the Terraform framework
# itself rejects `id = "..."` and `ignore_changes = [id]` is moot because
# nothing user-configured would ever set it.
_NEVER_IGNORE = {
    "id",
}


def _snake_to_camel(name: str) -> str:
    if "_" not in name:
        return name
    head, *rest = name.split("_")
    return head + "".join(p.title() for p in rest if p)


def _is_present(value: Any) -> bool:
    """A field counts as 'set in cloud' if it has any non-empty value."""
    return value not in (None, "", [], {}, 0, False)


def derive_lifecycle_ignores(cloud_data: dict, tf_type: str) -> List[str]:
    """Return the list of top-level TF-side attribute names that should be
    added to `lifecycle.ignore_changes`.

    Inputs
    ------
    cloud_data : dict
        Parsed cloud-JSON snapshot (post auto-scrub). Keys are camelCase as
        emitted by the GCP API; we look for both snake and camel variants.
    tf_type : str
        Terraform resource type, e.g. "google_compute_instance".

    Returns
    -------
    Sorted, de-duplicated list of TF-side (snake_case) field names.
    Empty list on any error or when the schema oracle has no entry for
    `tf_type`.
    """
    if not isinstance(cloud_data, dict):
        return []
    try:
        oracle = schema_oracle.get_oracle()
        if not oracle.has(tf_type):
            return []
    except Exception as e:  # noqa: BLE001 - fail open
        print(f"   - WARN: schema oracle unavailable for lifecycle planner ({e})")
        return []

    # Belt-and-braces: build the explicit pure-computed denylist so we can
    # assert no path slips through with the wrong flag combination. Putting a
    # pure-computed field in `lifecycle.ignore_changes` is a Terraform error
    # ("there can be no configured value to compare with"), so this guard is
    # critical even if the per-path flag check below is correct.
    pure_computed = set(oracle.computed_only_paths(tf_type))

    ignore: Set[str] = set()
    for path in oracle.list_paths(tf_type, kind="attribute"):
        # Top-level only — see module docstring for rationale.
        if "." in path:
            continue
        if path in pure_computed:
            continue  # never legal in ignore_changes
        if path in _NEVER_IGNORE:
            continue  # framework meta-attributes — see _NEVER_IGNORE
        info = oracle.get(tf_type, path)
        if info is None:
            continue
        # We want optional+computed (provider may overwrite). Skip pure-
        # computed (PR-3 already stripped them) and skip required (LLM
        # must emit). Skip deprecated.
        if not (info.computed and info.optional):
            continue
        if info.deprecated:
            continue
        # Is this attribute actually present in the cloud snapshot?
        for key in (path, _snake_to_camel(path)):
            if key in cloud_data and _is_present(cloud_data[key]):
                ignore.add(path)
                break

    return sorted(ignore)
