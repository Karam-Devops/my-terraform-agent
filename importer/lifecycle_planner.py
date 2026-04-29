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

from common.logging import get_logger

from . import schema_oracle

_log = get_logger(__name__)


# Framework meta-attributes that must NEVER appear in `lifecycle.ignore_changes`,
# even if the schema says they're `optional+computed`. Most provider schemas
# mark `id` as optional+computed (it's queryable), but the Terraform framework
# itself rejects `id = "..."` and `ignore_changes = [id]` is moot because
# nothing user-configured would ever set it.
_NEVER_IGNORE = {
    "id",
}


# PUI-1F v3.3 (2026-04-29 smoke 5): _KNOWN_NOISE_FIELDS retired.
# Was added in v3.1 as a per-type override that fed unconditional
# ignore-list hints to the LLM via IGNORE_LIST. Problem: the LLM
# could (and did) silently skip writing the lifecycle block if it
# didn't see the fields in the snapshot.
#
# Replaced by post_llm_overrides `lifecycle_ignore_changes` operation
# which deterministically INJECTS the lifecycle block post-LLM. See
# importer/post_llm_overrides.py:_inject_lifecycle_ignore_changes
# and the entries in importer/post_llm_overrides.json for
# google_cloud_run_v2_service and google_compute_disk.
#
# Empty dict kept so any external test mocks importing the symbol
# don't break. Future "always ignore" rules belong in
# post_llm_overrides.json, not here.
_KNOWN_NOISE_FIELDS: dict = {}


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
        _log.warning(
            "lifecycle_planner_oracle_unavailable",
            tf_type=tf_type,
            error=str(e),
        )
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

    # PUI-1F v3.1 known-noise overrides (see _KNOWN_NOISE_FIELDS).
    # Same _NEVER_IGNORE / pure_computed safety filters so a wrong
    # override entry can't crash terraform with "no configured
    # value to compare with."
    #
    # PUI-1F v3.2 (2026-04-29 smoke 5 fix): UNCONDITIONAL -- we no
    # longer require the field to be present in cloud_data. Previously
    # we mirrored the oracle-driven loop's "field present in cloud"
    # guard, but that broke for cloud_run_v2_service: the discovery
    # API doesn't return `client`/`clientVersion` in cloud_data, so
    # the override never fired, even though terraform import DID
    # populate them in state (different code path -- terraform reads
    # them via the v1 REST API directly). Result: HCL had no
    # ignore_changes block, plan diff'd, quarantine fired.
    #
    # Unconditional means: for any tf_type listed here, the named
    # fields ALWAYS land in lifecycle.ignore_changes. By definition
    # these are server-stamped metadata that the operator never
    # configures, so it's safe to ignore them blanket-style. A wrong
    # entry in this dict silently masks real drift -- still gated
    # on operator vigilance when adding entries.
    for noise_field in _KNOWN_NOISE_FIELDS.get(tf_type, []):
        if noise_field in _NEVER_IGNORE or noise_field in pure_computed:
            continue
        ignore.add(noise_field)
        _log.info(
            "lifecycle_planner_known_noise_added",
            tf_type=tf_type,
            field=noise_field,
            policy="unconditional",
        )

    return sorted(ignore)
