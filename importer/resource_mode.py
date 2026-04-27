# importer/resource_mode.py
"""
Mode detection and mode-specific pruning for cloud snapshots.

Some Terraform resources have "modes" — runtime configurations that make
large swaths of the schema either required or forbidden in ways the per-
attribute schema oracle cannot express. The provider only enforces these
at apply time, so the LLM has no way to know about them from the schema
alone. Examples:

  * `google_container_cluster` in Autopilot mode forbids `node_pool` /
    `node_config` / `cluster_autoscaling`; the schema lists all three as
    `optional` blocks but the provider rejects them.
  * `google_sql_database_instance` with `availability_type = REGIONAL`
    constrains replica fields.
  * `google_compute_instance` with `scheduling.preemptible = true` forces
    `automatic_restart = false`.

This module is the place to encode rules of the form
"if cloud says X, prune Y from JSON and tell the LLM Z."

Currently ships only:
  * `gke_autopilot` for `google_container_cluster`.

Adding new modes: append to `_MODES` below. Keep the structural data here;
keep the prompt instructions here too — the LLM should be told *what* to
do, not asked to detect anything.
"""

from typing import Any, Dict, List, Tuple

from . import snapshot_scrubber


# ---------------------------------------------------------------------------
# Detectors
# ---------------------------------------------------------------------------

def _gke_is_autopilot(d: Any) -> bool:
    """True iff the cluster snapshot reports Autopilot enabled.

    GCP exposes this under two key names depending on API version /
    `gcloud` version: `autopilotConfig.enabled` (newer) and
    `autopilot.enabled` (older). Accept either.
    """
    if not isinstance(d, dict):
        return False
    for key in ("autopilotConfig", "autopilot"):
        v = d.get(key)
        if isinstance(v, dict) and v.get("enabled") is True:
            return True
    return False


def _gke_is_standard(d: Any) -> bool:
    """True iff the cluster snapshot is GKE Standard (NOT Autopilot).

    P2-9: Standard-mode clusters have their own LLM-hallucination
    patterns that justify a separate mode addendum. The detector is
    deliberately the inverse of `_gke_is_autopilot` -- they're
    mutually exclusive on `google_container_cluster`. Both can be
    registered against the same `applies_to` because exactly one
    will match per snapshot.
    """
    if not isinstance(d, dict):
        return False
    return not _gke_is_autopilot(d)


# ---------------------------------------------------------------------------
# Mode registry
# ---------------------------------------------------------------------------
#
# Each entry:
#   applies_to        : Terraform resource type this mode is defined for.
#   detect            : callable(cloud_data: dict) -> bool.
#   prune_top_level   : list of cloud-JSON top-level keys to remove
#                       (both camelCase and snake_case variants — we strip
#                       both because the snapshot pipeline mixes them).
#   prompt_addendum   : extra block appended to the LLM system prompt AFTER
#                       the schema summary, in a high-visibility format.

_MODES: Dict[str, Dict[str, Any]] = {
    "gke_autopilot": {
        "applies_to": "google_container_cluster",
        "detect": _gke_is_autopilot,
        "prune_top_level": [
            # Managed-node-layer blocks Autopilot owns entirely
            "nodePools", "node_pools", "node_pool",
            "nodeConfig", "node_config",
            "clusterAutoscaling", "cluster_autoscaling",
            "defaultMaxPodsConstraint", "default_max_pods_constraint",
            "nodePoolDefaults", "node_pool_defaults",
            "nodePoolAutoConfig", "node_pool_auto_config",
            # Top-level attributes that conflict with enable_autopilot
            # (provider errors: "conflicts with enable_autopilot").
            "enableIntraNodeVisibility", "enable_intranode_visibility",
            "enableShieldedNodes", "enable_shielded_nodes",
            "enableKubernetesAlpha", "enable_kubernetes_alpha",
            "enableTpu", "enable_tpu",
            "enableLegacyAbac", "enable_legacy_abac",
            "loggingService", "logging_service",
            "monitoringService", "monitoring_service",
            "datapathProvider", "datapath_provider",
            "clusterIpv4Cidr", "cluster_ipv4_cidr",
            "defaultMaxPodsPerNode", "default_max_pods_per_node",
            "networkingMode", "networking_mode",
            "podSecurityPolicyConfig", "pod_security_policy_config",
            # P2-2: Autopilot manages node placement; manual `node_locations`
            # is rejected by the provider. We strip the snapshot field BEFORE
            # the LLM sees it so the post-LLM `locations` -> `node_locations`
            # rename (post_llm_overrides.json google_container_cluster) has
            # nothing to act on for Autopilot clusters. Standard clusters
            # keep the field and the rename converts the LLM's hallucinated
            # `locations` to the correct `node_locations` HCL form.
            "nodeLocations", "node_locations",
        ],
        # Nested paths to strip (dotted, snake_case; walker handles
        # camelCase automatically). Addons Autopilot manages and the
        # provider forbids under `enable_autopilot = true`.
        #
        # NOTE on `node_kubelet_config`: it lives under `node_pool_auto_config`
        # and `node_pool_defaults.node_config_defaults` in the snapshot. Both
        # parents are already in `prune_top_level` above, so the child gets
        # removed when the parent is. No separate entry needed here — a
        # redundant prune_paths entry would match nothing once the parent is
        # gone, and adding one anyway would suggest a source we've never
        # confirmed.
        "prune_paths": [
            "addons_config.dns_cache_config",
            "addons_config.network_policy_config",
            "addons_config.stateful_ha_config",
            "addons_config.config_connector_config",
            "addons_config.gke_backup_agent_config",
            # P2-9.1 hotfix: P2-9 placed this in `prune_top_level` but the
            # field actually lives at `monitoring_config.advanced_datapath_observability_config`
            # in the GCP API response (NOT at the cluster's top level). The
            # original placement was a no-op against any real Autopilot
            # snapshot because the field never appeared at the top level
            # to be matched. Surfaced by Phase 2 SMOKE 2 against poc-cluster:
            # the partial-empty `advanced_datapath_observability_config { }`
            # error STILL fired post-P2-9, indicating the prune didn't
            # touch anything. Moved to prune_paths (nested-dotted-path
            # mechanism via _strip_one_path) where it actually matches.
            #
            # Autopilot manages observability internally; the API returns
            # this nested block with content the LLM faithfully reflects
            # but the provider rejects with "argument enable_relay is
            # required" -- unfixable post-LLM because Autopilot owns the
            # value. Stripping at snapshot stage means the LLM never sees
            # it and never emits the partial-empty block.
            "monitoring_config.advanced_datapath_observability_config",
        ],
        "prompt_addendum": (
            "\n\n========================================================================\n"
            "MODE OVERRIDE - GKE AUTOPILOT CLUSTER\n"
            "========================================================================\n"
            "The cloud snapshot reports `autopilot.enabled = true`. Autopilot\n"
            "manages nearly everything about the node layer internally; the\n"
            "provider REJECTS at apply time any HCL that conflicts with it.\n"
            "\n"
            "REQUIRED:\n"
            "  * Add `enable_autopilot = true` to the resource body.\n"
            "  * Emit an `ip_allocation_policy { ... }` block — Autopilot\n"
            "    clusters are always VPC-native and the provider requires it.\n"
            "    (If the JSON has `ipAllocationPolicy` fields, use them;\n"
            "    otherwise emit an empty `ip_allocation_policy {}` block.)\n"
            "\n"
            "FORBIDDEN in Autopilot mode — DO NOT emit ANY of the following,\n"
            "regardless of what the input JSON contains. They conflict with\n"
            "`enable_autopilot` or are superseded by Autopilot's managed node\n"
            "layer:\n"
            "  Blocks:\n"
            "    - `node_pool { ... }`\n"
            "    - `node_config { ... }` (top-level)\n"
            "    - `cluster_autoscaling { ... }`\n"
            "    - `node_pool_defaults { ... }`\n"
            "    - `pod_security_policy_config { ... }`\n"
            "    - `default_snat_status { ... }` (only when conflicts surface)\n"
            "  Attributes:\n"
            "    - `cluster_ipv4_cidr` (use ip_allocation_policy instead)\n"
            "    - `default_max_pods_per_node`\n"
            "    - `enable_intranode_visibility`\n"
            "    - `enable_kubernetes_alpha`\n"
            "    - `enable_tpu`\n"
            "    - `enable_legacy_abac`\n"
            "    - `enable_shielded_nodes`\n"
            "    - `logging_service`\n"
            "    - `monitoring_service`\n"
            "    - `remove_default_node_pool`\n"
            "    - `initial_node_count`\n"
            "    - `networking_mode`\n"
            "    - `datapath_provider`\n"
            "\n"
            "Blocks you SHOULD still emit when the JSON has them:\n"
            "  * `release_channel`, `private_cluster_config`,\n"
            "    `ip_allocation_policy`, `network_policy`, `addons_config`,\n"
            "    `master_auth`, `workload_identity_config`,\n"
            "    `vertical_pod_autoscaling`, `binary_authorization`,\n"
            "    `maintenance_policy`, `authenticator_groups_config`,\n"
            "    `database_encryption`, `master_authorized_networks_config`,\n"
            "    `logging_config`, `monitoring_config`.\n"
            "\n"
            "The ROUND-TRIP FIDELITY rule from the schema block is OVERRIDDEN\n"
            "by the FORBIDDEN list above — if a field is forbidden here, do NOT\n"
            "write it even though the JSON has a value for it.\n"
            "========================================================================\n"
        ),
    },
    "gke_standard": {
        "applies_to": "google_container_cluster",
        "detect": _gke_is_standard,
        # Standard clusters DON'T need snapshot pruning today -- their issues
        # surface as LLM nesting hallucinations, not API/provider mismatches.
        # When real Standard-only schema mismatches appear, add entries here.
        "prune_top_level": [],
        "prompt_addendum": (
            "\n\n========================================================================\n"
            "MODE OVERRIDE - GKE STANDARD CLUSTER\n"
            "========================================================================\n"
            "This cluster is Standard mode (NOT Autopilot). The following block-\n"
            "nesting rules are commonly mis-applied by LLMs trained on mixed\n"
            "v1/v2 / Autopilot/Standard docs. Respect them strictly:\n"
            "\n"
            "TOP-LEVEL CLUSTER BLOCKS (place at the resource body root, NOT\n"
            "inside `node_pool { }` or `node_config { }`):\n"
            "  * `logging_config { ... }`\n"
            "  * `monitoring_config { ... }`\n"
            "  * `addons_config { ... }`\n"
            "  * `release_channel { ... }`\n"
            "  * `master_auth { ... }`\n"
            "  * `master_authorized_networks_config { ... }`\n"
            "  * `network_policy { ... }`\n"
            "  * `private_cluster_config { ... }`\n"
            "  * `vertical_pod_autoscaling { ... }`\n"
            "  * `workload_identity_config { ... }`\n"
            "  * `notification_config { ... }`\n"
            "  * `database_encryption { ... }`\n"
            "  * `binary_authorization { ... }`\n"
            "  * `mesh_certificates { ... }`\n"
            "  * `cost_management_config { ... }`\n"
            "\n"
            "BLOCKS THAT DO NOT EXIST in the cluster schema (LLM hallucinations\n"
            "from mixing field names) -- DO NOT EMIT under any name:\n"
            "  * `node_kubelet_config { ... }` -- there is no such block.\n"
            "    Kubelet config goes inside `node_pool { node_config {\n"
            "    kubelet_config { ... } } }`.\n"
            "  * `pod_security_policy_config { ... }` -- removed in Kubernetes\n"
            "    1.25; the field is no longer in the cluster schema.\n"
            "\n"
            "VALID NESTED BLOCKS inside `node_pool { }`:\n"
            "  * `node_config { }`           -- machine_type, disk_size_gb,\n"
            "                                    oauth_scopes, kubelet_config, etc.\n"
            "  * `autoscaling { }`           -- enabled, min_node_count, max_node_count\n"
            "  * `management { }`            -- auto_repair, auto_upgrade\n"
            "  * `network_config { }`        -- create_pod_range, pod_range\n"
            "  * `upgrade_settings { }`      -- max_surge, max_unavailable\n"
            "  * `placement_policy { }`      -- type, tpu_topology\n"
            "  * `queued_provisioning { }`   -- enabled\n"
            "If a block name in your input JSON doesn't match one of the above,\n"
            "it belongs at the top-level cluster body, not inside node_pool.\n"
            "========================================================================\n"
        ),
    },
}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def detect_modes(cloud_data: Any, tf_type: str) -> List[str]:
    """Return the mode IDs that match this snapshot for `tf_type`.

    Fail-safe: any per-mode detector exception is swallowed; the offending
    mode just doesn't activate.
    """
    out: List[str] = []
    for mode_id, spec in _MODES.items():
        if spec["applies_to"] != tf_type:
            continue
        try:
            if spec["detect"](cloud_data):
                out.append(mode_id)
        except Exception:  # noqa: BLE001 - one bad detector mustn't break the run
            continue
    return out


def apply_modes(cloud_data: dict, modes: List[str]) -> Tuple[dict, List[str]]:
    """Mutate `cloud_data` in place for every active mode:
      * drop every top-level key listed in that mode's `prune_top_level`;
      * drop every dotted nested path listed in `prune_paths` via
        `snapshot_scrubber.strip_paths` (camelCase / snake_case both OK).

    Returns `(cloud_data, dropped)` where `dropped` is a sorted,
    de-duplicated list of concrete cloud-JSON paths actually removed
    (e.g. `["addonsConfig.dnsCacheConfig", "enableShieldedNodes"]`).
    """
    dropped: List[str] = []
    if not isinstance(cloud_data, dict) or not modes:
        return cloud_data, dropped
    seen = set()
    for mode_id in modes:
        spec = _MODES.get(mode_id)
        if not spec:
            continue
        # Top-level key prune
        for key in spec.get("prune_top_level", []):
            if key in cloud_data and key not in seen:
                del cloud_data[key]
                dropped.append(key)
                seen.add(key)
        # Nested dotted-path prune (delegates to the path-aware walker)
        nested = spec.get("prune_paths") or []
        if nested:
            try:
                removed = snapshot_scrubber.strip_paths(cloud_data, nested)
            except Exception as _e:  # noqa: BLE001 - fail open
                removed = []
            for p in removed:
                if p not in seen:
                    dropped.append(p)
                    seen.add(p)
    return cloud_data, sorted(dropped)


def mode_prompt_addendum(modes: List[str]) -> str:
    """Return the concatenated prompt addenda for the given modes, or empty
    string if no modes are active or none have an addendum defined."""
    if not modes:
        return ""
    parts = [_MODES[m].get("prompt_addendum", "") for m in modes if m in _MODES]
    return "".join(p for p in parts if p)
