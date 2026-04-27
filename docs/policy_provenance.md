# Policy Rule Provenance Matrix

**Created:** 2026-04-27 (P4-PRE — pre-Phase-4 mining of archived
GoogleCloudPlatform/policy-library).

**Purpose.** Single source of truth for every Rego rule in
`policy/policies/`: where the rule's logic, thresholds, and patterns
came from, what public benchmark it maps to, and what Google's last
archived recommendation was. Three-source provenance per rule:

  1. **Google-archive** — the matching template in
     `GoogleCloudPlatform/policy-library` (archived 2025-08-20). What
     Google last published for this control. Apache 2.0 licensed; safe
     to mirror values + helper patterns.
  2. **CIS** — Center for Internet Security GCP Foundations Benchmark
     (or CIS Controls v8 for cross-cloud) section / control ID.
  3. **NIST** — SP 800-53 control family the rule implements.

When sources disagree (e.g. Google's archived default for KMS rotation
= 1y; CIS GCP = 90d), we default to the **stricter** value and cite
both.

The mining was not just numeric — every YAML template had an embedded
`rego: |` block with helper functions, defensive-defaulting patterns,
sentinel values, and field-path conventions worth adopting.

---

## Coverage at a glance

Total rules: **16** (9 GCP + 7 AWS).

| Rule | Google-archive | CIS | NIST | Status |
|---|---|---|---|---|
| common/mandatory_labels | gcp_enforce_labels_v1 | (FinOps consensus) | CM-8 | mined: header + asset-type list documented |
| common/mandatory_tags | — (no GCP equivalent; AWS sibling) | (FinOps consensus) | CM-8 | header only |
| google_compute_instance/gce_disk_encryption | gcp_cmek_settings_v1 (proxy) | CIS GCP 4.7 | SC-28, SC-12 | mined: header + KMS-key dimension future-work note |
| google_compute_instance/gce_no_public_ip | gcp_compute_external_ip_address (v2) | (CIS Controls v8 12.x) | SC-7 | mined: header + allowlist/denylist future-work note |
| google_compute_instance/gce_shielded_vm | gcp_gke_enable_shielded_nodes_v1 (proxy) | CIS GCP 4.9 | SI-7, CM-3 | mined: header + defensive-defaulting applied |
| google_storage_bucket/bucket_encryption | gcp_storage_cmek_encryption_v1 | (CIS Controls v8 3.11) | SC-28 | mined: header + helper-function pattern applied |
| google_storage_bucket/bucket_public_access | gcp_storage_bucket_world_readable_v1 + gcp_storage_bucket_policy_only_v1 | CIS GCP 5.1, 5.2 | AC-3, SC-7 | mined: header + bucketPolicyOnly dual-check + IAM-binding direct check applied |
| google_storage_bucket/bucket_retention | gcp_storage_bucket_retention_v1 | (NIST SI-12) | SI-12 | mined: header + max-retention future-work note |
| google_storage_bucket/bucket_versioning | — (no GCP equivalent) | (industry consensus) | SI-12, CP-9 | header only — Google's library never wrote this |
| google_compute_firewall/firewall_no_open_ssh | gcp_restricted_firewall_rules_v1 (derived) | CIS GCP 3.6 | SC-7 | **P4-5** — full rule, mining-derived |
| google_compute_firewall/firewall_no_open_rdp | gcp_restricted_firewall_rules_v1 (derived) | CIS GCP 3.7 | SC-7 | **P4-5** — sibling of SSH, port 3389 |
| google_compute_firewall/firewall_logs_enabled | gcp_network_enable_firewall_logs_v1 | CIS GCP 3.x | AU-12 | **P4-5** — logConfig.enable check |
| google_compute_disk/disk_cmek_required | gcp_cmek_settings_v1 (proxy) | CIS GCP 4.7 | SC-28, SC-12 | **P4-5** — disk-side CMEK |
| google_compute_disk/disk_snapshot_policy_attached | gcp_compute_disk_resource_policies_v1 (derived) | (industry consensus) | CP-9 | **P4-5** — non-empty resourcePolicies |
| google_compute_network/network_no_default_vpc | gcp_network_restrict_default_v1 | CIS GCP 3.1 | SC-7 | **P4-5** — name == "default" deny |
| google_compute_network/network_routing_mode_regional | gcp_network_routing_v1 (derived) | (industry consensus) | SC-7 | **P4-5** — REGIONAL stricter than Google's GLOBAL default |
| google_compute_subnetwork/subnet_flow_logs_enabled | gcp_network_enable_flow_logs_v1 | CIS GCP 3.8 | AU-12 | **P4-5** — logConfig.enable + legacy enableFlowLogs, exempts proxy subnets |
| google_compute_subnetwork/subnet_private_google_access | gcp_network_enable_private_google_access_v1 | (industry consensus) | SC-7 | **P4-5** — privateIpGoogleAccess required |
| google_container_cluster/cluster_workload_identity | gcp_gke_enable_workload_identity_v1 | CIS GCP 7.x | IA-5 | **P4-6** — workloadPool OR identityNamespace |
| google_container_cluster/cluster_private_endpoint | gcp_gke_private_cluster_v1 | CIS GCP 7.x | SC-7 | **P4-6** — both enablePrivateNodes + enablePrivateEndpoint |
| google_container_cluster/cluster_legacy_abac_disabled | gcp_gke_legacy_abac_v1 | CIS GCP 7.x | AC-3 | **P4-6** — RBAC over ABAC |
| google_container_cluster/cluster_master_authorized_networks | gcp_gke_master_authorized_networks_enabled_v1 | CIS GCP 7.x | SC-7 | **P4-6** — masterAuthorizedNetworksConfig.enabled |
| google_container_node_pool/node_pool_auto_upgrade | gcp_gke_node_auto_upgrade_v1 | CIS GCP 7.x | SI-2 | **P4-6** — management.autoUpgrade |
| google_container_node_pool/node_pool_auto_repair | gcp_gke_node_auto_repair_v1 | CIS GCP 7.x | SI-2 | **P4-6** — management.autoRepair |
| google_container_node_pool/node_pool_uses_cos | gcp_gke_container_optimized_os | (industry consensus) | CM-7 | **P4-6** — imageType in {COS, COS_CONTAINERD} |
| google_container_node_pool/node_pool_no_default_sa | gcp_gke_disable_default_service_account_v1 | CIS GCP 7.x | AC-6 | **P4-6** — config.serviceAccount != "default" |
| google_kms_crypto_key/key_rotation_max_90_days | gcp_cmek_rotation_v1 | CIS GCP 1.10 | SC-12 | **P4-6** — STRICTER than Google's 1y default |
| google_kms_crypto_key/key_protection_level_hsm | gcp_cmek_settings_v1 | (industry consensus) | SC-12, SC-13 | **P4-6** — versionTemplate.protectionLevel == HSM |
| google_sql_database_instance/sql_no_public_ip | gcp_sql_public_ip_v1 | CIS GCP 6.5 | SC-7 | **P4-7** — settings.ipConfiguration.ipv4Enabled |
| google_sql_database_instance/sql_ssl_required | gcp_sql_ssl_v1 | CIS GCP 6.4 | SC-8 | **P4-7** — settings.ipConfiguration.requireSsl |
| google_sql_database_instance/sql_backup_enabled | gcp_sql_backup_v1 | CIS GCP 6.7 | CP-9 | **P4-7** — settings.backupConfiguration.enabled |
| google_cloud_run_v2_service/cloudrun_no_public_invoker | NONE (Cloud Run not in archive) | CIS Controls v8 6.x | AC-3 | **P4-7** — roles/run.invoker not bound to allUsers |
| google_cloud_run_v2_service/cloudrun_min_instances_documented | NONE (Cloud Run not in archive) | (industry consensus) | SA-3 | **P4-7** — template.scaling.minInstanceCount must be set |
| google_pubsub_topic/pubsub_topic_cmek_required | NONE (Pub/Sub not in archive) | CIS Controls v8 3.11 | SC-28 | **P4-7** — kmsKeyName non-empty |
| google_pubsub_topic/pubsub_topic_iam_no_allusers | NONE (Pub/Sub not in archive) | (industry consensus) | AC-3 | **P4-7** — no allUsers in iam_policy.bindings |
| google_pubsub_subscription/pubsub_sub_dead_letter_configured | NONE (Pub/Sub not in archive) | (industry consensus) | SI-11 | **P4-7** — deadLetterPolicy.deadLetterTopic non-empty |
| google_pubsub_subscription/pubsub_sub_iam_no_allusers | NONE (Pub/Sub not in archive) | (industry consensus) | AC-3 | **P4-7** — no allUsers in iam_policy.bindings |
| aws_instance/ec2_ebs_encryption | — (AWS) | CIS AWS 2.2.1 | SC-28 | header only |
| aws_instance/ec2_imds_v2 | — (AWS) | CIS AWS 5.6 | AC-3 | header only |
| aws_instance/ec2_no_public_ip | — (AWS) | (CIS Controls v8 12.x) | SC-7 | header only |
| aws_s3_bucket/s3_bucket_encryption | — (AWS) | CIS AWS 2.1.1 | SC-28 | header only |
| aws_s3_bucket/s3_bucket_public_access | — (AWS) | CIS AWS 2.1.5 | AC-3, SC-7 | header only |
| aws_s3_bucket/s3_bucket_retention | — (AWS) | (NIST SI-12) | SI-12 | header only |
| aws_s3_bucket/s3_bucket_versioning | — (AWS) | CIS AWS 2.1.3 | SI-12, CP-9 | header only |

---

## Per-rule extraction details

### 1. common/mandatory_labels.rego

**Google template:** `gcp_enforce_labels_v1.yaml`
(`GCPEnforceLabelConstraintV1`)

**Extracted properties:**

  * **Per-asset-type label-storage paths.** Google's helper
    `get_labels()` knows that labels live in different paths per
    asset type. We currently assume every GCP asset puts labels at
    top-level `labels` map; that's wrong for these:
      * CloudSQL (`sqladmin.googleapis.com/Instance`): labels under
        `settings.userLabels`
      * GKE Cluster (`container.googleapis.com/Cluster`): labels under
        `resourceLabels`
      * Spanner (`spanner.googleapis.com/Instance`): labels under
        `labels` (same as default but Google special-cased it)
    **Status:** documented as Phase 4 candidate; not yet applied
    (would change the rule's snapshot-input contract).

  * **Default scan list of asset types** (Google's recommendation
    for which types should require labels):
    ```
    cloudresourcemanager.googleapis.com/Project
    storage.googleapis.com/Bucket
    compute.googleapis.com/Instance
    compute.googleapis.com/Image
    compute.googleapis.com/Disk
    compute.googleapis.com/Snapshot
    bigtableadmin.googleapis.com/Instance
    sqladmin.googleapis.com/Instance
    dataproc.googleapis.com/Job
    dataproc.googleapis.com/Cluster
    container.googleapis.com/Cluster
    bigquery.googleapis.com/Dataset
    bigquery.googleapis.com/Table
    spanner.googleapis.com/Instance
    ```
    Useful as the seed list for our CG-2 (Detector + Policy coverage
    parity) work.

  * **Regex value-pattern matching.** Google's schema is
    `mandatory_labels: [{label_key: regex_pattern}]` — not just
    presence checks but value-format validation. E.g. enforce
    `env` matches `^(dev|staging|prod)$`.
    **Status:** Phase 4 candidate (significant new feature; would
    change rule interface).

**CIS:** No specific CIS GCP control for labels — labels are a
FinOps + ops-hygiene concern. Cite "industry consensus" + NIST
CM-8.

**NIST:** SP 800-53 CM-8 (System Component Inventory) — labels
are an asset-tracking control.

---

### 2. common/mandatory_tags.rego

**Google template:** none — AWS-only rule. GCP equivalent is
`mandatory_labels` (sibling rule); AWS uses different snapshot shape
(`Tags = [{Key, Value}]` list vs GCP's `labels = {k: v}` map).

**CIS:** "FinOps consensus" — same reasoning as mandatory_labels.
CIS AWS Foundations Benchmark doesn't have a tagging rule either.

**NIST:** CM-8.

---

### 3. google_compute_instance/gce_disk_encryption.rego

**Google template:** `gcp_cmek_settings_v1.yaml`
(`GCPCMEKSettingsConstraintV1`) — **proxy match**: Google's template
targets `cloudkms.googleapis.com/CryptoKey` directly; ours targets
the consuming `compute.googleapis.com/Instance`'s disks. Different
validation surface but same intent.

**Extracted properties:**

  * **Configurable KMS dimensions** Google validates on the key
    itself:
      * `protection_level` — `SOFTWARE` vs `HSM` (HSM = FIPS 140-2
        Level 3)
      * `algorithm` — e.g. `GOOGLE_SYMMETRIC_ENCRYPTION` vs others
      * `purpose` — e.g. `ENCRYPT_DECRYPT`, `ASYMMETRIC_SIGN`
      * `rotation_period` — default `31536000s` (1 year)
    **Status:** these check the KMS key, not the disk. Future Phase 4
    rule candidate: add a `google_kms_crypto_key/` package with these
    dimension checks.

  * **Sentinel pattern:** `99999999s` = "never rotates". When
    `rotationPeriod` field is absent from the asset, Google's helper
    returns this sentinel so the missing-rotation case naturally
    triggers a violation. Useful pattern.

**CIS:** **CIS GCP 4.7** — "Ensure VM disks for critical VMs are
encrypted with Customer-Supplied Encryption Keys (CSEK) or
Customer Managed Encryption Keys (CMEK)".

**NIST:** SP 800-53 SC-28 (Protection of Information at Rest) +
SC-12 (Cryptographic Key Establishment).

---

### 4. google_compute_instance/gce_no_public_ip.rego

**Google template:** `gcp_compute_external_ip_address.yaml`
(`GCPComputeExternalIpAccessConstraintV2`)

**Extracted properties:**

  * **Field paths confirmed identical** to ours:
    `instance.networkInterfaces[_].accessConfigs`. Convergent design
    — both correct.

  * **CIS annotation in metadata:** Google's template carries
    `benchmark: CIS11_5.03` (the only template in the sample with a
    benchmark annotation — most don't). CIS11 = CIS Controls v1.1,
    section 5.03 = "Implement Application Layer Filtering" (network
    boundary). Worth noting but not the strongest control mapping.

  * **Allowlist/denylist parameterization.** Google supports four
    modes:
      * `mode: allowlist, match_mode: exact` — only listed VMs may
        have public IPs
      * `mode: denylist, match_mode: exact` — listed VMs may NOT
        have public IPs
      * `mode: allowlist, match_mode: regex` — regex variant
      * `mode: denylist, match_mode: regex` — regex variant
    Currently we use a hardcoded `tags.items[] == "internet-facing"`
    exception. Google's model is more flexible.
    **Status:** Phase 4 candidate (would change rule interface).
    Documented in rule comment.

**CIS:** No direct CIS GCP rule numbered for "no public IP" (the
nearest are CIS GCP 3.6 / 3.7 for firewall ingress). Cite CIS
Controls v8 12.X (Network Infrastructure Management).

**NIST:** SP 800-53 SC-7 (Boundary Protection).

---

### 5. google_compute_instance/gce_shielded_vm.rego

**Google template:** `gcp_gke_enable_shielded_nodes_v1.yaml`
(`GCPGKEEnableShieldedNodesConstraintV1`) — **proxy match**: only
GKE-shielded variant exists in the archived library; no generic
compute shielded VM template.

**Extracted properties:**

  * **Defensive defaulting pattern** via `lib.get_default(asset,
    "field", default)`. Without this, our current `not
    input.shieldedInstanceConfig.enableSecureBoot` may behave
    inconsistently when `shieldedInstanceConfig` itself is absent
    (vs `null` vs `{}`). The pattern:
    ```rego
    sic := object.get(input, "shieldedInstanceConfig", {})
    secure_boot := object.get(sic, "enableSecureBoot", false)
    secure_boot == false
    ```
    Behaves identically across "absent block", "explicit
    `enabled=false`", "null".
    **Status:** APPLIED — rewritten using OPA's built-in
    `object.get()` (equivalent to Google's `lib.get_default()`).

  * **Notable:** Google requires only `enableSecureBoot` AND
    `enableIntegrityMonitoring`. They DON'T require `enableVtpm`. We
    are stricter (require all three). Documented in header.

  * **Two-path validation:** Google checks BOTH cluster-level
    `shieldedNodes.enabled` AND each node_pool's individual
    `config.shieldedInstanceConfig`. For composite resources this
    is a useful pattern — a future GKE shielded-nodes rule should
    mirror it.

**CIS:** **CIS GCP 4.9** — "Ensure Compute instances have Shielded
VM enabled".

**NIST:** SP 800-53 SI-7 (Software, Firmware, and Information
Integrity) + CM-3 (Configuration Change Control).

---

### 6. google_storage_bucket/bucket_encryption.rego

**Google template:** `gcp_storage_cmek_encryption_v1.yaml`
(`GCPStorageCMEKEncryptionConstraintV1`)

**Extracted properties:**

  * **Helper-function with defensive defaulting:**
    ```rego
    default_kms_key_name(bucket) := name {
        encryption := object.get(bucket, "encryption", {})
        name := object.get(encryption, "defaultKmsKeyName", "")
    }
    deny[...] {
        default_kms_key_name(input) == ""
        ...
    }
    ```
    Catches both "no encryption block" AND "encryption block but no
    key" uniformly. Our current `not input.encryption.defaultKmsKeyName`
    works but is fragile across `null` vs absent vs `{}` shapes.
    **Status:** APPLIED.

  * **Metadata in violation details:** Google returns the value
    in violation metadata so debug shows what the field actually
    contained.

**CIS:** No direct CIS GCP control for bucket-level CMEK (covered
under broader "encrypt at rest"). Cite **CIS Controls v8 3.11**
(Encrypt Sensitive Data at Rest).

**NIST:** SP 800-53 SC-28.

---

### 7. google_storage_bucket/bucket_public_access.rego

**Google templates:** TWO matches — both worth absorbing:

  * `gcp_storage_bucket_world_readable_v1.yaml`
    (`GCPStorageBucketWorldReadableConstraintV1`)
  * `gcp_storage_bucket_policy_only_v1.yaml`
    (`GCPStorageBucketPolicyOnlyConstraintV1`)

**Extracted properties:**

  * **Public-principal canonical strings** (we don't currently
    check IAM bindings directly):
      * `"allUsers"` — completely public
      * `"allAuthenticatedUsers"` — any authenticated Google user
        (still essentially public)
    **Status:** APPLIED — added a new deny rule that scans
    `iam_policy.bindings[].members[]` for these two strings.
    Defense-in-depth: even if UBLA + PAP look right, a bound
    `allUsers` member is the smoking gun.

  * **Dual-API check for UBLA.** Google's `policy_only_v1`
    template checks BOTH:
      * `iamConfiguration.bucketPolicyOnly.enabled` (deprecated
        original API, still set on older buckets)
      * `iamConfiguration.uniformBucketLevelAccess.enabled` (current
        renamed API)
    A bucket that has the legacy field set but not the modern one
    IS still UBLA-protected; our current rule (modern-only) would
    false-fire. **Status:** APPLIED — rule denies only when BOTH
    are unset/false.

  * **Exemption pattern:** Google parameterizes an `exemptions: []`
    list. **Status:** Phase 4 candidate.

**CIS:** **CIS GCP 5.1** ("Ensure Cloud Storage bucket is not
anonymously or publicly accessible") + **CIS GCP 5.2** ("Ensure
Cloud Storage buckets have uniform bucket-level access enabled"). Two
controls in one rule.

**NIST:** SP 800-53 AC-3 (Access Enforcement) + SC-7 (Boundary
Protection).

---

### 8. google_storage_bucket/bucket_retention.rego

**Google template:** `gcp_storage_bucket_retention_v1.yaml`
(`GCPStorageBucketRetentionConstraintV1`)

**Extracted properties:**

  * **Different control axis.** Google's template covers OBJECT
    LIFECYCLE retention (`lifecycle.rule[].action.type == "Delete"`
    + age conditions). Ours covers SOFT-DELETE retention
    (`softDeletePolicy.retentionDurationSeconds` — the recovery
    window after explicit delete). Both are valid + complementary
    controls; we keep ours, and a Phase 4 rule could add Google's
    coverage as a sibling.

  * **Bidirectional bounds.** Google parameterizes BOTH
    `minimum_retention_days` AND `maximum_retention_days`. The max
    is interesting — beyond X days, retention costs balloon
    (cost-control angle, not just security).
    **Status:** Phase 4 candidate — adding a max bound.

  * **Time conversion constant:** `ns_in_days = ((((24 * 60) * 60)
    * 1000) * 1000) * 1000`. Useful if we ever need RFC3339 → days
    arithmetic.

**CIS:** No specific CIS GCP control. NIST SI-12 covers
"Information Management and Retention".

**NIST:** SP 800-53 SI-12.

---

### 9. google_storage_bucket/bucket_versioning.rego

**Google template:** **NONE** — Google's archived library never
wrote a versioning rule. Notable absence; we keep ours sourced from
industry consensus.

**CIS:** No direct CIS GCP control (CIS AWS 2.1.3 has the AWS
sibling). Cite "industry consensus / data-loss prevention".

**NIST:** SP 800-53 SI-12 + CP-9 (System Backup).

---

### 10. google_compute_firewall/firewall_no_open_ssh.rego (P4-5)

* **Source:** `gcp_restricted_firewall_rules_v1.yaml` (derived).
  Google's template is fully parameterized; we hardcode the
  SSH-from-internet case (CIS GCP 3.6 names this specific rule).
  Field paths + sentinel values mined verbatim.
* **Mined sentinels:** `"0.0.0.0/0"`, `"all"` (wildcard
  IPProtocol), port range syntax `"lo-hi"`, missing `ports` =
  all open.
* **CIS:** GCP 3.6.  **NIST:** SC-7.
* **Helpers** in `_helpers.rego` (sibling): `default_list`,
  `firewall_enabled`, `sources_open_to_internet`, `permits_tcp`,
  `permits_port`, `allows_tcp_port_to_world` -- shared with
  `firewall_no_open_rdp.rego` and any future port-specific rules.

### 11. google_compute_firewall/firewall_no_open_rdp.rego (P4-5)

* **Source:** same as #10 -- `gcp_restricted_firewall_rules_v1`.
* Sibling of #10; reuses every helper. Port `"3389"` instead of
  `"22"`. **CIS:** GCP 3.7. **NIST:** SC-7.

### 12. google_compute_firewall/firewall_logs_enabled.rego (P4-5)

* **Source:** `gcp_network_enable_firewall_logs_v1.yaml`.
* **Mined field path:** `logConfig.enable` (defaults false). Deny
  when not explicitly true. **CIS:** GCP 3.x (firewall logging).
  **NIST:** AU-12 (Audit Generation).

### 13. google_compute_disk/disk_cmek_required.rego (P4-5)

* **Source:** `gcp_cmek_settings_v1.yaml` (proxy match: ours
  targets the disk's `diskEncryptionKey.kmsKeyName`; Google's
  targets the CryptoKey directly).
* Helper `disk_kms_key_name` mirrors `bucket_encryption.rego`'s
  `default_kms_key_name` pattern from P4-PRE. **CIS:** GCP 4.7.
  **NIST:** SC-28, SC-12.

### 14. google_compute_disk/disk_snapshot_policy_attached.rego (P4-5)

* **Source:** `gcp_compute_disk_resource_policies_v1.yaml`
  (derived). Google's template is parameterized
  (allowlist/denylist of policy URLs); we hardcode the floor:
  "non-empty `resourcePolicies` list".
* **CIS:** No specific control. **NIST:** CP-9 (System Backup) --
  the rule's intent (force scheduled snapshots).

### 15. google_compute_network/network_no_default_vpc.rego (P4-5)

* **Source:** `gcp_network_restrict_default_v1.yaml`.
* **Mined sentinel:** `name == "default"` identifies the
  auto-created default VPC (which carries permissive firewall
  rules pre-applied). **CIS:** GCP 3.1. **NIST:** SC-7.

### 16. google_compute_network/network_routing_mode_regional.rego (P4-5)

* **Source:** `gcp_network_routing_v1.yaml`.
* **Mined field path:** `routingConfig.routingMode`. Allowed
  values: `"REGIONAL"`, `"GLOBAL"`. Google's archive default was
  GLOBAL (or operator-parameterized); we choose REGIONAL for
  tighter blast radius. Cite both in provenance.
* **CIS:** No specific control. **NIST:** SC-7.

### 17. google_compute_subnetwork/subnet_flow_logs_enabled.rego (P4-5)

* **Source:** `gcp_network_enable_flow_logs_v1.yaml`.
* **Mined field paths:** `logConfig.enable` (modern) AND
  `enableFlowLogs` (legacy). Both checked because Google's
  template does -- older subnetworks may carry the legacy field.
* **Mined exemption:** `purpose` in
  `{"REGIONAL_MANAGED_PROXY", "INTERNAL_HTTPS_LOAD_BALANCER"}`
  -- control-plane subnets without traffic flow to log.
* **CIS:** GCP 3.8. **NIST:** AU-12.

### 18. google_compute_subnetwork/subnet_private_google_access.rego (P4-5)

* **Source:** `gcp_network_enable_private_google_access_v1.yaml`.
* **Mined field path:** `privateIpGoogleAccess` (bool). Deny when
  false / absent. **NIST:** SC-7.

### 19. google_container_cluster/cluster_workload_identity.rego (P4-6)

* **Source:** `gcp_gke_enable_workload_identity_v1.yaml`.
* **Mined field paths:** `workloadIdentityConfig.workloadPool` (current
  API) AND `identityNamespace` (legacy beta). Both checked because
  Google's template does. **CIS:** GCP 7.x. **NIST:** IA-5.

### 20. google_container_cluster/cluster_private_endpoint.rego (P4-6)

* **Source:** `gcp_gke_private_cluster_v1.yaml`.
* **Mined field paths:** `privateClusterConfig.enablePrivateNodes` +
  `enablePrivateEndpoint`. Two-tier deny: HIGH severity if nodes are
  public; MED if nodes private but master endpoint exposed.
  **CIS:** GCP 7.x. **NIST:** SC-7.

### 21. google_container_cluster/cluster_legacy_abac_disabled.rego (P4-6)

* **Source:** `gcp_gke_legacy_abac_v1.yaml`.
* **Mined field path:** `legacyAbac.enabled`. Deny when true (RBAC
  is the modern replacement). **CIS:** GCP 7.x. **NIST:** AC-3.

### 22. google_container_cluster/cluster_master_authorized_networks.rego (P4-6)

* **Source:** `gcp_gke_master_authorized_networks_enabled_v1.yaml`.
* **Mined field path:** `masterAuthorizedNetworksConfig.enabled`
  must be true (else API server reachable from 0.0.0.0/0).
  **CIS:** GCP 7.x. **NIST:** SC-7.

### 23. google_container_node_pool/node_pool_auto_upgrade.rego (P4-6)

* **Source:** `gcp_gke_node_auto_upgrade_v1.yaml`.
* **Mined field path:** `management.autoUpgrade`. Per-node-pool shape
  (our snapshot fetches each node pool individually; Google's template
  iterates `nodePools[]` from the cluster asset). **CIS:** GCP 7.x.
  **NIST:** SI-2.

### 24. google_container_node_pool/node_pool_auto_repair.rego (P4-6)

* **Source:** `gcp_gke_node_auto_repair_v1.yaml`.
* **Mined field path:** `management.autoRepair`. Same per-node-pool
  shape as #23. **CIS:** GCP 7.x. **NIST:** SI-2.

### 25. google_container_node_pool/node_pool_uses_cos.rego (P4-6)

* **Source:** `gcp_gke_container_optimized_os.yaml`.
* **Mined allowed values:** `{"COS", "COS_CONTAINERD"}` for
  `config.imageType`. COS = Container-Optimized OS (read-only root,
  no package manager). **NIST:** CM-7 (Least Functionality).

### 26. google_container_node_pool/node_pool_no_default_sa.rego (P4-6)

* **Source:** `gcp_gke_disable_default_service_account_v1.yaml`.
* **Mined sentinel:** `config.serviceAccount == "default"` flags use
  of the default Compute SA (broad project-Editor scopes by default).
  **CIS:** GCP 7.x. **NIST:** AC-6.
* **Scope-mapping note:** Google's template targets the cluster +
  iterates node pools; our snapshot fetcher returns per-node-pool
  shape so the rule applies directly. The original P4-PRE plan had
  this as a `google_service_account/*` rule but our importer doesn't
  enumerate ServiceAccountKey resources -- this gives the same
  security signal at the right snapshot layer.

### 27. google_kms_crypto_key/key_rotation_max_90_days.rego (P4-6)

* **Source:** `gcp_cmek_rotation_v1.yaml`.
* **Mined defaults vs CIS divergence**: Google's archive default =
  `31536000s` (1 year). CIS GCP 1.10 recommends 90 days. We choose
  STRICTER (90d = 7,776,000s) and cite both in the provenance + deny
  message for transparency.
* **Mined sentinel:** `"99999999s"` for "never rotates" -- by
  parsing as a huge number the inequality trips automatically.
* **CIS:** GCP 1.10. **NIST:** SC-12.

### 28. google_kms_crypto_key/key_protection_level_hsm.rego (P4-6)

* **Source:** `gcp_cmek_settings_v1.yaml`.
* **Mined field path:** `versionTemplate.protectionLevel`. Allowed
  values per Google: `SOFTWARE`, `HSM`, `EXTERNAL`. We hardcode HSM
  as the floor (Google's archive parameterizes).
* **NIST:** SC-12, SC-13.

---

## AWS rules (no GCP-archive provenance possible)

For each AWS rule, the GCP-archive line of provenance is "—"; the CIS
+ NIST citations come from the CIS AWS Foundations Benchmark and
NIST SP 800-53 directly. Headers added for traceability + so a
future maintainer reading the AWS rules can cross-reference the GCP
sibling immediately.

| Rule | CIS AWS | NIST | GCP sibling |
|---|---|---|---|
| ec2_ebs_encryption | 2.2.1 | SC-28 | google_compute_instance/gce_disk_encryption |
| ec2_imds_v2 | 5.6 | AC-3 | (no direct GCP analogue) |
| ec2_no_public_ip | (Controls v8 12.x) | SC-7 | google_compute_instance/gce_no_public_ip |
| s3_bucket_encryption | 2.1.1 | SC-28 | google_storage_bucket/bucket_encryption |
| s3_bucket_public_access | 2.1.5 | AC-3, SC-7 | google_storage_bucket/bucket_public_access |
| s3_bucket_retention | (NIST SI-12) | SI-12 | google_storage_bucket/bucket_retention |
| s3_bucket_versioning | 2.1.3 | SI-12, CP-9 | google_storage_bucket/bucket_versioning |

---

## What this commit applies vs documents

**Functional improvements applied** (real code changes):

  1. `gce_shielded_vm`: defensive `object.get()` defaulting
  2. `bucket_encryption`: helper-function with defensive defaulting +
     metadata in violation
  3. `bucket_public_access`: dual UBLA-API check (legacy +
     modern) + new deny rule for `allUsers` /
     `allAuthenticatedUsers` in `iam_policy.bindings`

**Documented as Phase 4 candidates** (changes the rule interface, so
not applied here):

  1. `mandatory_labels`: regex value-pattern matching
  2. `mandatory_labels`: per-asset-type label-storage paths
  3. `gce_no_public_ip`: allowlist/denylist parameterization
  4. `bucket_retention`: max-retention bidirectional bound
  5. `bucket_public_access`: parameterized exemption list

**Documented as new-rule candidates** (different validation surface,
deserves its own file):

  1. `google_kms_crypto_key/` package: KMS protection_level +
     algorithm + purpose + rotation_period checks
  2. `google_storage_bucket/bucket_object_lifecycle`: Google's
     lifecycle Delete rule (sibling to soft-delete retention)

**Provenance headers added to all 16 rules** with the three-source
attribution block.

---

## Coverage gap: importer types without Rego rules

**Added 2026-04-27.** Importer supports 17 GCP resource types
(`importer/config.py:TF_TYPE_TO_GCLOUD_INFO`). Only 2 currently
have Rego rules. The 15-type gap is the work item under CG-2
(Detector + Policy coverage parity); the per-type breakdown
including which GCP-archive templates to mine for each lives in
`docs/saas_readiness_punchlist.md` CG-2 spec.

| Importer tf_type | Rules today | GCP-archive templates available |
|---|---|---|
| google_compute_instance | 3 | mined P4-PRE; more available |
| google_compute_disk | 0 | 2 (cmek_settings + disk_resource_policies) |
| google_compute_firewall | 0 | 2 (restricted_firewall_rules + firewall_logs) |
| google_compute_address | 0 | 0 (industry consensus only) |
| google_compute_network | 0 | 2 (network_routing + network_restrict_default) |
| google_compute_subnetwork | 0 | 2 (network_enable_flow_logs + private_google_access) |
| google_compute_instance_template | 0 | inherits instance |
| google_container_cluster | 0 | **14** (richest archived coverage in the library) |
| google_container_node_pool | 0 | 4 (node_auto_repair + auto_upgrade + allowed_node_sa + container_optimized_os) |
| google_service_account | 0 | 3 (sa_creation + sa_key_age + sa_key_type) |
| google_storage_bucket | 4 | mined P4-PRE; 2 more available (logging + location) |
| google_sql_database_instance | 0 | **7** (backup, maintenance_window, public_ip, ssl, world_readable, allowed_authorized_networks, instance_type) |
| google_kms_key_ring | 0 | inherits crypto_key |
| google_kms_crypto_key | 0 | 2 (cmek_rotation default 1y vs CIS 90d; cmek_settings) |
| google_cloud_run_v2_service | 0 | **NONE** (Cloud Run not covered in archived library) |
| google_pubsub_topic | 0 | **NONE** |
| google_pubsub_subscription | 0 | **NONE** |

**Mining methodology** for each new rule (when the type has
archive templates available): apply the same 4-step approach used
in P4-PRE for the existing 9 GCP rules:

  1. Fetch the YAML template's embedded `rego: |` block.
  2. Identify reusable properties: helper functions, defensive
     defaulting patterns (`object.get` / `lib.get_default`),
     numeric defaults, sentinel values, canonical strings,
     asset-field paths.
  3. Convert the asset-field paths from CAI shape
     (`input.asset.resource.data.*`) to our snapshot shape
     (`input.*`) since we evaluate against snapshots, not CAI
     proto.
  4. Add three-source provenance header: Google-archive template
     + last-published default, CIS GCP / Controls section, NIST
     SP 800-53 family.

**For types with no archived template** (Cloud Run, Pub/Sub):
source defaults from CIS Controls v8 + industry consensus +
Google's CURRENT public Best Practices documentation (which is
maintained even though the policy-library was archived).

---

## Maintaining this matrix

Update this doc whenever:

  * A new rule is added to `policy/policies/` — add an entry with
    its three-source provenance.
  * A new CIS Benchmark version ships — re-check the section
    numbers for any changes.
  * A field-path or value-default in a rule changes — update the
    provenance header in the rule + the matrix row here so they
    stay in sync.

The matrix doubles as a customer-facing artifact eventually: the
SaaS UI's "Why this rule fires" detail pane reads its content from
here.
