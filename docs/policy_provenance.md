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
