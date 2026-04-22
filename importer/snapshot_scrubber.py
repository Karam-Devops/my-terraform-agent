# importer/snapshot_scrubber.py
"""
Auto-scrub cloud snapshots before the LLM sees them.

Strips every attribute the schema oracle marks as *pure-computed*
(`computed=True` AND NOT `optional` AND NOT `required`). These are read-only
provider outputs — the LLM has no business writing them, and historically did
write them, which is half of why `heuristics.json` exists.

Scope of this module
--------------------
* Pure-computed only. Does NOT touch optional+computed fields (those become
  `lifecycle.ignore_changes` in PR-4) and obviously does NOT touch any
  user-settable fields.
* Path-aware: `network_interface.name` strips the NIC name *inside the
  network interface*, not the top-level instance name (which happens to share
  the leaf "name").
* CamelCase-aware: a snake_case schema path matches its camelCase cloud-JSON
  twin (`creation_timestamp` → `creationTimestamp`).
* Plural-alias-aware: handles the small set of TF-provider singular→plural
  renames the GCP API uses (`network_interface` → `networkInterfaces`,
  `service_account` → `serviceAccounts`, etc.).

Fail-safe: any unexpected exception returns the input unchanged with an empty
strip list. The downstream heuristics-driven `scrub_json` still runs after
this and can clean up anything we missed.
"""

import json
from typing import Any, List, Tuple

from . import schema_oracle


# ---------------------------------------------------------------------------
# Auto-label filter (PR-6)
# ---------------------------------------------------------------------------
#
# GCP services attach their own labels to resources behind the user's back:
#   * `goog-*`            (e.g. `goog-ec-src`, `goog-managed-by`)
#   * `goog-k8s-*`        (GKE-cluster-managed nodes)
#   * `gke-*`             (GKE node pool internals)
#   * `k8s-io-*`          (k8s-managed objects on the GCP side)
#   * `goog-managed-*`    (any service-managed resource)
#
# These come back from `gcloud describe` and land in our snapshot's `labels`
# block. If we round-trip them into HCL, the next `terraform plan` shows them
# as "user-set" — but the provider also reports them as service-managed, so
# every plan shows a no-op diff. Strip them at the snapshot stage before
# either the LLM or the lifecycle-planner sees them.
#
# This is intentionally a tiny, well-known prefix list. We do NOT want to
# strip user labels that happen to start with "g".
_AUTO_LABEL_PREFIXES = (
    "goog-",
    "gke-",
    "k8s-io-",
)
# Some GCP services use unprefixed but unmistakably-internal names.
_AUTO_LABEL_EXACT = {
    "managed-by-cnrm",
}
# Containers in the snapshot that are themselves a labels-like map.
# (Keys we look at for filtering; both snake and camel variants get hit.)
_LABEL_CONTAINERS = ("labels", "resource_labels", "resourceLabels")


def _is_auto_label(key: str) -> bool:
    if key in _AUTO_LABEL_EXACT:
        return True
    return any(key.startswith(p) for p in _AUTO_LABEL_PREFIXES)


def filter_auto_labels(resource_json_str: str) -> Tuple[str, List[str]]:
    """Strip provider-managed labels from every labels-like map in the JSON.

    Walks the snapshot recursively and, on every dict it finds, drops keys
    matching the auto-label patterns from any of the known label containers
    (`labels`, `resource_labels`, etc.). Other keys are untouched.

    Returns
    -------
    (filtered_json_str, dropped_paths)
        `dropped_paths` lists the concrete cloud-JSON paths removed
        (e.g. `["labels.goog-ec-src", "node_pools[0].config.labels.gke-..."]`),
        useful for logging. Empty when nothing was removed.

    Fail-safe: on parse error returns the input unchanged.
    """
    try:
        data = json.loads(resource_json_str)
    except (json.JSONDecodeError, TypeError):
        return resource_json_str, []

    dropped: List[str] = []

    def _walk(node: Any, trail: str) -> None:
        if isinstance(node, dict):
            for key in list(node.keys()):
                child = node[key]
                child_trail = f"{trail}.{key}" if trail else key
                if key in _LABEL_CONTAINERS and isinstance(child, dict):
                    for lk in list(child.keys()):
                        if _is_auto_label(lk):
                            del child[lk]
                            dropped.append(f"{child_trail}.{lk}")
                _walk(child, child_trail)
        elif isinstance(node, list):
            for i, item in enumerate(node):
                _walk(item, f"{trail}[{i}]")

    _walk(data, "")
    if not dropped:
        return resource_json_str, []
    return json.dumps(data, indent=2), dropped


# ---------------------------------------------------------------------------
# Naming helpers
# ---------------------------------------------------------------------------

def _snake_to_camel(name: str) -> str:
    """foo_bar_baz -> fooBarBaz. Idempotent on already-camel strings."""
    if "_" not in name:
        return name
    head, *rest = name.split("_")
    return head + "".join(p.title() for p in rest if p)


# Cloud-key plural aliases. The Google TF provider exposes these GCP API
# plurals as singular HCL block names; the path index in the schema oracle
# uses the singular form. When walking the cloud JSON we must accept the
# plural form too.
#
# Keep this list small and intentional — it should track FIELD_ALIASES in
# detector/config.py. Anything outside this set is matched only by snake/
# camel variants of the schema path segment itself.
_PLURAL_ALIASES = {
    "network_interface": "networkInterfaces",
    "service_account":   "serviceAccounts",
    "access_config":     "accessConfigs",
    "alias_ip_range":    "aliasIpRanges",
    "ipv6_access_config": "ipv6AccessConfigs",
}


# Top-level keys that must ALWAYS be stripped from any cloud snapshot,
# regardless of what the per-resource schema oracle says.
#
# `id` is the canonical case: Terraform treats it as a framework meta-
# attribute on every resource (you can never assign to it), but most
# provider schemas — including Google's — mark it as `optional+computed`
# rather than pure-computed. So PR-3's schema-driven strip leaves it
# alone, the LLM faithfully copies the GCP-side ID into HCL, and
# `terraform plan` rejects the file with `Invalid or unknown key`.
#
# We strip these only at the TOP level — a nested `id` (e.g. inside a
# `network_interface` access_config) is part of that block's schema and
# may legitimately be user-settable on some resources.
_ALWAYS_STRIP_TOP_LEVEL = {
    "id",
}


# Provider-dropped paths: attributes / blocks that the GCP API still
# returns (for back-compat) but the current `terraform providers schema`
# no longer lists. Writing them to HCL yields `Unsupported block type`
# or `Unsupported argument` at plan time.
#
# These are DOTTED schema-style paths (snake_case). The walker handles
# camelCase cloud-JSON spelling automatically. Per-resource scoping is
# by top-level segment — so `addons_config.kubernetes_dashboard` only
# fires on resources that actually have an `addons_config` block.
#
# Grow this list one entry per confirmed import failure. Do NOT add
# speculative entries — a wrong entry silently drops real user data.
_PROVIDER_DROPPED_PATHS: List[str] = [
    # GKE addons retired by the Google provider. GKE itself dropped the
    # Kubernetes Dashboard UI years ago; the API still echoes it back.
    "addons_config.kubernetes_dashboard",
    "addons_config.istio_config",
    "addons_config.kalm_config",
    # GKE `ipAllocationPolicy` legacy-duplicate CIDR keys. The API returns
    # BOTH `clusterIpv4Cidr` and `clusterIpv4CidrBlock` (same for services);
    # the TF provider only has the `_block` variant inside the block. The
    # path walker matches exact segments, so stripping `cluster_ipv4_cidr`
    # here does NOT touch the sibling `cluster_ipv4_cidr_block`.
    "ip_allocation_policy.cluster_ipv4_cidr",
    "ip_allocation_policy.services_ipv4_cidr",
    # GKE `ipAllocationPolicy` cidr_block / secondary_range_name mutual
    # exclusion. The provider rejects writing both — they describe the
    # same range from two angles (CIDR vs. named subnet range). The API
    # returns both because the cluster has both materialized. For an
    # imported existing cluster, the *named range* is the canonical
    # representation (the range physically exists with that name; the
    # CIDR is derivable). Drop the `_block` form so the LLM keeps the
    # name. Only relevant to `google_container_cluster`; safe globally
    # because the path lives only on that resource.
    "ip_allocation_policy.cluster_ipv4_cidr_block",
    "ip_allocation_policy.services_ipv4_cidr_block",
    # `ip_allocation_policy.use_ip_aliases`: GCP API field that has no
    # equivalent inside the provider's `ip_allocation_policy` block. The
    # mere presence of the block IS the modern "use IP aliases" signal;
    # the legacy top-level `enable_ip_aliases` is deprecated.
    "ip_allocation_policy.use_ip_aliases",
    # `maintenance_policy.resource_version`: GCP API etag for optimistic
    # concurrency on the maintenance window. Not a TF field. Stripping it
    # leaves `maintenance_policy` empty for clusters with no real config,
    # which PR-12's empty-top-level-key cleanup then drops entirely.
    "maintenance_policy.resource_version",
    # `master_authorized_networks_config.enabled`: GCP API uses an explicit
    # `enabled: true` boolean to signal the feature is active. The TF schema
    # encodes the same signal as block-presence (the block exists ⇒ enabled),
    # so writing `enabled = true` inside the HCL block produces
    # `Unsupported argument`. The bug only surfaces on RE-imports after a
    # `terraform apply` materialized the block server-side with all default
    # fields populated — the first import after cluster creation often
    # doesn't return `enabled` at all. Strip unconditionally so neither path
    # ever produces broken HCL.
    "master_authorized_networks_config.enabled",
]


def _candidate_keys(snake_segment: str) -> set:
    """All cloud-JSON key spellings that could match this schema-path segment."""
    out = {snake_segment, _snake_to_camel(snake_segment)}
    if snake_segment in _PLURAL_ALIASES:
        out.add(_PLURAL_ALIASES[snake_segment])
    return out


# ---------------------------------------------------------------------------
# Provider-dropped paths filter (PR-11)
# ---------------------------------------------------------------------------

def filter_provider_dropped_paths(resource_json_str: str) -> Tuple[str, List[str]]:
    """Strip known provider-dropped paths (see `_PROVIDER_DROPPED_PATHS`).

    Walks the snapshot once per configured path and deletes the leaf when
    it matches. Uses the same `_strip_one_path` descent as the schema-
    driven auto-scrubber, so camelCase / snake_case / plural-alias
    spellings are all handled.

    Returns
    -------
    (filtered_json_str, dropped_paths)
        `dropped_paths` lists concrete cloud-JSON paths removed
        (e.g. `["addonsConfig.kubernetesDashboard"]`). Empty when
        nothing matched.

    Fail-safe: on JSON parse error returns the input unchanged.
    """
    try:
        data = json.loads(resource_json_str)
    except (json.JSONDecodeError, TypeError):
        return resource_json_str, []

    modified: List[str] = []
    try:
        for path in _PROVIDER_DROPPED_PATHS:
            parts = path.split(".")
            modified.extend(_strip_one_path(data, parts, trail=""))
    except Exception as e:  # noqa: BLE001 - fail open
        print(f"   - WARN: provider-dropped-paths filter failed, leaving JSON untouched ({e})")
        return resource_json_str, []

    if not modified:
        return resource_json_str, []
    return json.dumps(data, indent=2), modified


# ---------------------------------------------------------------------------
# Path stripper
# ---------------------------------------------------------------------------

def _strip_one_path(obj: Any, parts: List[str], trail: str) -> List[str]:
    """Walks `obj` following the dotted path `parts`. Deletes the leaf when
    reached. Returns concrete cloud-JSON paths that were modified.

    Importantly, this only descends through the *matched* branch — it does
    not blindly recurse into siblings. A computed-only path of
    `network_interface.name` will only delete `name` keys that live inside a
    network-interface element, never a top-level instance `name`.
    """
    if not parts or obj is None:
        return []
    head, rest = parts[0], parts[1:]
    candidates = _candidate_keys(head)
    modified: List[str] = []

    if isinstance(obj, dict):
        for key in list(obj.keys()):
            if key not in candidates:
                continue
            if not rest:
                # Leaf — strip it.
                del obj[key]
                modified.append(f"{trail}.{key}" if trail else key)
            else:
                child = obj[key]
                child_trail = f"{trail}.{key}" if trail else key
                if isinstance(child, list):
                    # Lists: each element is a separate "row" of the block.
                    # The dotted schema path treats `network_interface.name`
                    # as a single descent, so we recurse into items with the
                    # *remaining* parts, not the same parts.
                    for i, item in enumerate(child):
                        modified.extend(
                            _strip_one_path(item, rest, f"{child_trail}[{i}]")
                        )
                else:
                    modified.extend(_strip_one_path(child, rest, child_trail))
    return modified


# ---------------------------------------------------------------------------
# Drop empty top-level keys (PR-12)
# ---------------------------------------------------------------------------

def drop_empty_top_level_keys(resource_json_str: str) -> Tuple[str, List[str]]:
    """Remove top-level keys whose value collapsed to a no-op.

    After the prune passes (auto-scrub, label filter, provider-dropped,
    mode prune) some snapshot keys end up as `{}` / `[]` / `None`. The
    LLM sees a key in the JSON and emits a `block {}` for it; provider
    blocks with required inner fields then reject the empty block at
    plan time (e.g. `maintenance_policy {}` requires one of
    `daily_maintenance_window` / `recurring_window`).

    Top-level only — recursing would risk cascading deletes that strip
    legitimate intentionally-empty nested config like `master_auth.client_certificate_config`.

    Returns
    -------
    (filtered_json_str, dropped_keys)
        `dropped_keys` lists top-level keys removed. Empty when nothing
        matched. Fail-safe on parse error.
    """
    try:
        data = json.loads(resource_json_str)
    except (json.JSONDecodeError, TypeError):
        return resource_json_str, []
    if not isinstance(data, dict):
        return resource_json_str, []

    dropped: List[str] = []
    for key in list(data.keys()):
        v = data[key]
        if v is None or v == {} or v == [] or v == "":
            del data[key]
            dropped.append(key)

    if not dropped:
        return resource_json_str, []
    return json.dumps(data, indent=2), dropped


# ---------------------------------------------------------------------------
# Public path-stripping helper (used by resource_mode.py for nested pruning)
# ---------------------------------------------------------------------------

def strip_paths(obj: Any, paths: List[str]) -> List[str]:
    """Strip every dotted snake_case path in `paths` from `obj` in place.

    Thin public wrapper around `_strip_one_path`. Same semantics:
      * snake_case / camelCase / plural-alias spellings all match
      * descends only the matched branch (no sibling clobbering)
      * lists are entered element-by-element

    Returns concrete cloud-JSON paths actually removed (e.g.
    `["addonsConfig.dnsCacheConfig", "enableIntraNodeVisibility"]`).
    Empty when nothing matched. Mutates `obj` in place.
    """
    modified: List[str] = []
    if obj is None or not paths:
        return modified
    for path in paths:
        parts = path.split(".")
        modified.extend(_strip_one_path(obj, parts, trail=""))
    return modified


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def auto_scrub_cloud_snapshot(
    resource_json_str: str,
    tf_type: str,
) -> Tuple[str, List[str]]:
    """Strip every pure-computed attribute from a cloud-JSON snapshot.

    Parameters
    ----------
    resource_json_str
        Raw JSON string from `gcloud ... describe`. Camel-cased.
    tf_type
        Terraform resource type, e.g. "google_compute_instance".

    Returns
    -------
    (scrubbed_json_str, stripped_paths)
        `stripped_paths` is the concrete cloud-JSON paths that were removed
        (e.g. `["selfLink", "networkInterfaces[0].name"]`), useful for
        logging. Empty when nothing was removed.

    On any error returns `(resource_json_str, [])` so the caller pipeline
    continues unchanged.
    """
    try:
        oracle = schema_oracle.get_oracle()
    except Exception as e:  # noqa: BLE001 - intentional broad catch, fail open
        print(f"   - WARN: schema oracle unavailable, skipping auto-scrub ({e})")
        return resource_json_str, []

    try:
        # Even if the oracle has no entry for this resource, we still strip
        # framework meta-attributes like top-level `id`. So parse first, then
        # decide whether to also do the schema-driven pass.
        data = json.loads(resource_json_str)
        all_modified: List[str] = []

        # Always-strip pass (framework meta-attributes; see _ALWAYS_STRIP_TOP_LEVEL).
        if isinstance(data, dict):
            for key in list(data.keys()):
                if key in _ALWAYS_STRIP_TOP_LEVEL:
                    del data[key]
                    all_modified.append(key)

        if oracle.has(tf_type):
            computed_only = oracle.computed_only_paths(tf_type)
            for path in computed_only:
                parts = path.split(".")
                all_modified.extend(_strip_one_path(data, parts, trail=""))

        if not all_modified:
            return resource_json_str, []
        return json.dumps(data, indent=2), all_modified
    except Exception as e:  # noqa: BLE001
        print(f"   - WARN: auto-scrub failed for {tf_type}, leaving JSON untouched ({e})")
        return resource_json_str, []


# ---------------------------------------------------------------------------
# CLI smoke-test:
#   python -m importer.snapshot_scrubber google_compute_instance < snapshot.json
# ---------------------------------------------------------------------------

def _cli(argv: List[str]) -> int:
    import sys
    if len(argv) < 2:
        print("Usage: python -m importer.snapshot_scrubber <tf_type> < snapshot.json")
        return 2
    tf_type = argv[1]
    raw = sys.stdin.read()
    if not raw.strip():
        print("ERROR: read empty input from stdin", file=sys.stderr)
        return 2
    scrubbed, stripped = auto_scrub_cloud_snapshot(raw, tf_type)
    print(f"# stripped {len(stripped)} field(s):", file=sys.stderr)
    for p in stripped:
        print(f"#   - {p}", file=sys.stderr)
    print(scrubbed)
    return 0


if __name__ == "__main__":
    import sys as _sys
    _sys.exit(_cli(_sys.argv))
