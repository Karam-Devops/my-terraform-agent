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


def _always_true(d: Any) -> bool:
    """Detector that fires for every snapshot of the registered tf_type.

    Used by modes whose addendum / prune list applies universally to
    all instances of a resource type, not conditionally on snapshot
    content. P2-11 example: gke_node_pool mode's prompt addendum
    applies to every node pool regardless of cluster mode (Autopilot
    pools fail at describe so the addendum only effectively reaches
    Standard pools, but we don't need to encode that condition --
    the describe failure short-circuits before the mode applies).
    """
    return isinstance(d, dict)


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
            # P4-13: Ray Operator addon -- Autopilot manages it internally
            # and rejects manual configuration. Migrated from the legacy
            # heuristics.json (where the rule was a SNIPPET that emitted
            # `ray_operator_config { enabled = false }` -- which Autopilot
            # also rejects). The right behaviour is to STRIP the field
            # entirely so the LLM never sees it and never emits a block
            # that the provider will reject.
            "addons_config.ray_operator_config",
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
        "prune_top_level": [
            # P2-10: SMOKE 2 surfaced provider-rejection of poc-cluster-std
            # because the LLM emitted BOTH `cluster_ipv4_cidr` (legacy
            # top-level field) AND `ip_allocation_policy { ... }` (modern
            # VPC-native block). Provider error: "cluster_ipv4_cidr
            # conflicts with ip_allocation_policy". Modern Standard
            # clusters ALWAYS have ip_allocation_policy (VPC-native is
            # required for new clusters since 2022), so cluster_ipv4_cidr
            # is universally redundant on real-world Standard snapshots.
            #
            # Caveat: very-old legacy non-VPC-native clusters (pre-2022,
            # rare) DO need cluster_ipv4_cidr because they lack
            # ip_allocation_policy. Stripping unconditionally loses
            # config for those. Acceptable trade-off for now -- if a
            # customer reports a legacy cluster import failure we'll
            # add conditional logic (only prune when ip_allocation_policy
            # is also present in the snapshot). Punchlist if it surfaces.
            "clusterIpv4Cidr", "cluster_ipv4_cidr",
        ],
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
    "gke_node_pool": {
        # P2-11: addresses LLM nesting hallucinations on
        # google_container_node_pool. Surfaced by SMOKE 2 against
        # poc-cluster-std/default-pool: LLM emitted `cgroup_mode =
        # "CGROUP_MODE_V2"` directly inside `node_config { }` instead
        # of inside `node_config.linux_node_config { }`. Provider
        # rejects with "argument named cgroup_mode is not expected here".
        # Same nesting-confusion pattern as P2-9 for clusters; this mode
        # generalises the fix to the node pool resource type.
        #
        # Detector is _always_true so the addendum is injected for
        # every node_pool snapshot. Autopilot node pools never reach
        # this code path (their describe is blocked by Google API
        # before mode detection) so the mode effectively applies only
        # to Standard pool imports.
        "applies_to": "google_container_node_pool",
        "detect": _always_true,
        # No snapshot pruning yet -- the bug is LLM mis-nesting, not an
        # API/provider mismatch. Add prune entries here if a Standard
        # pool field surfaces as universally-rejected.
        "prune_top_level": [],
        "prompt_addendum": (
            "\n\n========================================================================\n"
            "MODE OVERRIDE - GKE NODE POOL NESTING\n"
            "========================================================================\n"
            "node_config has TWO levels of inner blocks. Some fields go DIRECTLY\n"
            "in node_config; others go inside its `linux_node_config` /\n"
            "`windows_node_config` / `gvnic` / `kubelet_config` sub-blocks.\n"
            "Putting a field at the wrong level produces 'Unsupported argument'.\n"
            "\n"
            "FIELDS THAT GO IN `node_config { linux_node_config { ... } }`:\n"
            "  * `cgroup_mode = \"CGROUP_MODE_V2\"`  (Linux cgroup version)\n"
            "  * `sysctls = { ... }`                (Linux kernel parameters)\n"
            "  * `hugepages_config { }`             (Linux hugepages)\n"
            "\n"
            "FIELDS THAT GO IN `node_config { kubelet_config { ... } }`:\n"
            "  * `cpu_manager_policy`\n"
            "  * `cpu_cfs_quota`\n"
            "  * `pod_pids_limit`\n"
            "  * `insecure_kubelet_readonly_port_enabled`\n"
            "\n"
            "FIELDS THAT GO DIRECTLY IN `node_config { ... }`:\n"
            "  * `machine_type`, `disk_size_gb`, `disk_type`, `image_type`\n"
            "  * `service_account`, `oauth_scopes`, `metadata`\n"
            "  * `labels`, `tags`, `taint`, `resource_labels`\n"
            "  * `preemptible`, `spot`, `min_cpu_platform`\n"
            "  * `boot_disk_kms_key`, `enable_confidential_storage`\n"
            "\n"
            "RULE OF THUMB: if the field name starts with a Linux-kernel concept\n"
            "(cgroup, sysctl, hugepages) it goes in `linux_node_config`. If it's\n"
            "a kubelet-tuning concept (cpu_manager, pod_pids, kubelet_*) it goes\n"
            "in `kubelet_config`. Otherwise it goes directly in `node_config`.\n"
            "========================================================================\n"
        ),
    },
    "compute_instance_default": {
        # P4-13: replaces two legacy heuristics.json OMIT rules for
        # google_compute_instance:
        #   guest_os_features: OMIT
        #   resource_policies: OMIT
        # Both were SNIPPET-class workarounds that the snapshot_scrubber
        # couldn't catch via schema_oracle (the fields are NOT
        # computed-only -- they have real values that the LLM happily
        # echoes back, but the provider rejects them at apply time on
        # the standard import path). Pre-LLM strip via this mode is
        # the right architectural home -- mirrors how P2-10 handles
        # cluster_ipv4_cidr for gke_standard.
        #
        # Detector is _always_true; applies to every compute_instance
        # snapshot.
        "applies_to": "google_compute_instance",
        "detect": _always_true,
        "prune_top_level": [
            # Computed feature flags Google sets on the source image
            # (e.g. {"type": "VIRTIO_SCSI_MULTIQUEUE"}). The LLM emits
            # them as a HCL block; provider rejects ('not expected
            # here' on standard import flow). Per-VM customisation
            # would need a separate explicit field, which we don't
            # support yet.
            "guestOsFeatures", "guest_os_features",
            # Snapshot/backup schedules attached to the disk via
            # resource_policies. The LLM emits them as a top-level
            # list; provider expects them at disk-resource level via
            # google_compute_resource_policy + a separate
            # google_compute_disk_resource_policy_attachment. Fork
            # for a future commit if customers need round-trip.
            "resourcePolicies", "resource_policies",
        ],
        "prompt_addendum": (
            "\n\n========================================================================\n"
            "MODE OVERRIDE - COMPUTE INSTANCE DEFAULT\n"
            "========================================================================\n"
            "Two cloud-side fields are stripped from the input snapshot before\n"
            "you see it (no need to reproduce them in HCL):\n"
            "  * `guest_os_features` -- computed from the source image; not\n"
            "    settable on the instance resource.\n"
            "  * `resource_policies`  -- attached via separate\n"
            "    google_compute_disk_resource_policy_attachment resource,\n"
            "    not on the instance.\n"
            "If the input JSON does NOT carry these fields, you don't need to\n"
            "do anything special; this addendum just documents why.\n"
            "========================================================================\n"
        ),
    },
    "cloud_run_v2_default": {
        # P4-11: re-surfaced in SMOKE 4 on poc-cloudrun. The cloud snapshot
        # carries v1-vestige fields (`startupCpuBoost`) at the template
        # level that the v2 provider rejects ("argument is not expected
        # here"). Same v1-vestige class as `container_concurrency`
        # (P2-8) and `latest_revision` (P2-8) -- both already protected
        # by post_llm_overrides. P4-11 adds the missing third entry +
        # ALSO adds pre-LLM scrub here so the LLM never sees the field
        # to copy from in the first place. Defense in depth.
        #
        # Detector is _always_true so the addendum + pruning apply to
        # every cloud_run_v2_service snapshot. There's no v1 vs v2 mode
        # split (v2 is always v2); the mode entry is the cleanest place
        # in the existing infrastructure to hang per-tf_type "always
        # strip these v1 vestiges" rules.
        "applies_to": "google_cloud_run_v2_service",
        "detect": _always_true,
        "prune_top_level": [
            # Top-level v1-vestige in some snapshots (rare; defensive).
            "startupCpuBoost", "startup_cpu_boost",
            # v1-only routing flag at top level (older snapshots).
            "latestRevision", "latest_revision",
            # v1's container_concurrency at top level (rare; usually
            # nests inside template).
            "containerConcurrency", "container_concurrency",
        ],
        "prune_paths": [
            # Primary placement: nested under template in v2 cloud
            # snapshots. The walker handles camelCase / snake_case both.
            "template.startup_cpu_boost",
            "template.container_concurrency",
            "template.latest_revision",
        ],
        "prompt_addendum": (
            "\n\n========================================================================\n"
            "MODE OVERRIDE - CLOUD RUN v2 SERVICE\n"
            "========================================================================\n"
            "This is a google_cloud_run_v2_service (v2 schema). The legacy v1\n"
            "resource (google_cloud_run_service) had several fields that v2\n"
            "either renamed, relocated, or removed entirely. The cloud snapshot\n"
            "may still carry the legacy field names; do NOT echo them back.\n"
            "\n"
            "FORBIDDEN in v2 -- DO NOT emit ANY of the following, at any nesting\n"
            "level, regardless of what the input JSON contains:\n"
            "  * `startup_cpu_boost`     -- v1 vestige; v2 relocated this concept\n"
            "                              under `template.containers[].startup_probe`.\n"
            "  * `container_concurrency` -- v1 placement; v2 uses\n"
            "                              `template.max_instance_request_concurrency`.\n"
            "  * `latest_revision = true`-- v1 routing flag; v2 traffic block uses\n"
            "                              `type = \"TRAFFIC_TARGET_ALLOCATION_TYPE_LATEST\"`.\n"
            "\n"
            "REQUIRED v2 patterns:\n"
            "  * `template.scaling.{min_instance_count, max_instance_count}` --\n"
            "    explicit declaration documents intent (also satisfies our\n"
            "    cloudrun_min_instances_documented policy rule).\n"
            "  * `traffic { type = \"TRAFFIC_TARGET_ALLOCATION_TYPE_LATEST\", percent = 100 }`\n"
            "    -- v2 string-typed allocation, NOT v1's boolean.\n"
            "  * Health probes (startup_probe, liveness_probe) live INSIDE\n"
            "    `template.containers[]`, NOT at template-level (v1's placement).\n"
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
