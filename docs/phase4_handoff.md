# Phase 4 Handoff (updated 2026-04-27 mid-session)

**Purpose.** Captures Phase 4 progress + all decisions + concrete next
steps so a fresh session can pick up cleanly without re-deriving
patterns or re-asking the user for choices already made.

---

## Where we are (commits landed)

```
c411682 Phase 4 P4-8: CG-3 walker test -- enforce provenance across all 44 rules  <- HEAD
efa0d78 Phase 4 P4-7: CG-2 SQL + Cloud Run + Pub/Sub rules wave (9 new)
92b033c Phase 4 P4-6: CG-2 GKE + KMS rules wave (10 new)
0ca059d Phase 4 P4-5: CG-2 Compute + Network rules wave (8 new)
37cfe18 Phase 4 P4-5 preview: 1 firewall rule + helpers + structural test
453819c docs: Phase 4 mid-session handoff -- patterns + remaining work spec
f815762 Phase 4 P4-4: CG-2 part A -- IN_SCOPE expansion + drift-stub gating
5c6fb06 Phase 4 P4-3: CG-1 DriftReport + Detector.rescan() unmanaged-tracking
a25bea9 Phase 4 P4-2: extract importer.inventory(project_id) for Detector reuse
11a6a85 Phase 4 P4-1: detector + policy hygiene combo
d2279c5 P4-PRE follow-up: 15-type policy coverage gap with mining map
06274fa P4-PRE: mine GoogleCloudPlatform/policy-library + apply to 16 regos
```

**Test totals: 558 green + 379 subtests** (105 importer + 53 translator
+ 80 detector + 252 policy/common). Run the full suite with:

```
python -m pytest detector/tests policy/tests common/tests -q
python -m unittest discover -s importer/tests -p "test_*.py"
python -m unittest discover -s translator/tests -p "test_*.py"
```

---

## Phase 4 plan (11 commits; 8 done, 3 remaining)

| # | Status | Commit | Description |
|---|---|---|---|
| P4-1 | ✅ | `11a6a85` | Detector + Policy hygiene combo |
| P4-2 | ✅ | `a25bea9` | importer.inventory() extraction |
| P4-3 | ✅ | `5c6fb06` | DriftReport + Detector.rescan() |
| P4-4 | ✅ | `f815762` | IN_SCOPE_TF_TYPES 2 → 17 + drift-stub gating |
| P4-5 preview | ✅ | `37cfe18` | 1 firewall rule + helpers + test pattern |
| P4-5 | ✅ | `0ca059d` | Compute + Network rules wave (8 more rules; 9 total in wave) |
| P4-6 | ✅ | `92b033c` | GKE + KMS rules wave (10 new rules) |
| P4-7 | ✅ | `efa0d78` | SQL + Cloud Run + Pub/Sub rules (9 new rules) |
| P4-8 | ✅ | `c411682` | CG-3 walker test (264 subtests across 44 rules) |
| P4-9a | ✅ | `0f8378b` | CC-9 scaffolding + loader + 3 priority golden examples |
| P4-9b | ✅ | `0a35740` | CC-9 remaining 7 golden examples (test fixture extension) |
| P4-9 hotfix | ✅ | `38aed9e` | gitignore exception for golden_examples/*.tf |
| **P4-10** | manual | — | SMOKE 4 prep doc shipped; user runs the manual smoke |

**Phase 4 SUBSTANTIALLY COMPLETE.** All 10 dev/code commits landed. Last
step is the manual SMOKE 4 the user executes per
`docs/smoke4_prep.md` -- prep doc + retro template ready.

CG-1 + CG-2 + CG-3 + CC-9 all shipped. 44 Rego rules across 14 dirs
all carrying three-source provenance auto-validated by the P4-8
walker. 10 hand-written golden HCL examples for the importer's top
10 hallucination-prone types.

---

## Established conventions (user-approved at P4-5 preview checkpoint)

These are LOCKED IN — don't re-ask the user:

### 1. Compact 4-line provenance header

Every Rego rule starts with:

```rego
# <rule_name>.rego
# Source: <archived template name> [derived?]   (or "no GCP equivalent")
# Standard: CIS <id> | NIST SP 800-53 <family>
# Default: <chosen value> (rationale: stricter than Google / CIS-aligned / etc.)
# See docs/policy_provenance.md for full mining details.
```

Verbose mining details (sentinel-value catalog, helper-pattern
derivation, future-work flags) live in `docs/policy_provenance.md`
per-rule sections, NOT inline in the rule file.

### 2. Shared helpers per-tf_type

Once a directory gets ≥2 sibling rules, extract shared logic into
`<dir>/_helpers.rego`. Convention: filename starts with `_` (visually
distinct); conftest loads it identically (still `package main`,
helpers accessible to all sibling rules in the dir).

Example: `policy/policies/google_compute_firewall/_helpers.rego`
holds `default_list`, `firewall_enabled`, `sources_open_to_internet`,
`permits_tcp`, `permits_port`, `allows_tcp_port_to_world`. The SSH
rule uses `allows_tcp_port_to_world("22")`, RDP will use
`allows_tcp_port_to_world("3389")`, etc.

### 3. Standardized deny message format

```
[SEVERITY][rule_id] <noun> %s <action_failed> -- <suggested_fix> (CIS <id>)
```

Operator sees the audit reference inline without opening the rule
file. Examples:

```
[HIGH][firewall_no_open_ssh] firewall %s allows SSH (TCP/22) ingress from 0.0.0.0/0 -- restrict sourceRanges (CIS GCP 3.6)
[HIGH][bucket_encryption] bucket %s has no customer-managed encryption key (encryption.defaultKmsKeyName must be set)
```

### 4. Naming convention

`<action_shape>.rego` (NOT `cis_<id>_*.rego`). Reads naturally in
violation messages + grep. CIS ID lives in provenance + message text.

### 5. Structural-only tests for P4-5/6/7

No local conftest/opa, so semantic tests defer to P4-10 SMOKE.
Per-rule structural tests verify:
  * file exists
  * `package main`
  * has `deny[msg]`
  * 3-line provenance (Source / Standard / Default; NIST is on Standard line)
  * CIS control ID present
  * NIST family present
  * severity + rule_id message prefix
  * control ID suffix in message

P4-8 generalizes these checks via a single walker test that scans
every `.rego` in `policy/policies/` (excluding `_helpers.rego`).

---

## Per-rule mining workflow (cookbook for the next ~24 rules)

For each rule:

1. **Look up** the mapping in `docs/saas_readiness_punchlist.md` CG-2
   spec table -- per-type, lists which archived template(s) to mine
   from + suggested rule names + CIS control IDs.

2. **Fetch the archived template** via WebFetch:
   ```
   https://raw.githubusercontent.com/GoogleCloudPlatform/policy-library/main/policies/templates/<name>.yaml
   ```
   Extract: field paths, sentinel values, helper patterns, parameter
   defaults.

3. **Convert input shape**: Google's templates use
   `input.asset.resource.data.*` (CAI shape); ours use `input.*`
   (snapshot shape). Most field names are identical after the
   `asset.resource.data.` prefix is stripped.

4. **Write the rule** following the compact format above. Keep deny
   logic minimal -- delegate to helpers (extract to `_helpers.rego`
   when ≥2 rules in dir share logic).

5. **Update `docs/policy_provenance.md`**:
   * Add row to the matrix (top of file)
   * Add per-rule entry (numbered) in compact form (~10 lines max)

6. **Add structural test** in `policy/tests/test_<tf_type>.py` (one
   file per dir). Pattern from
   `policy/tests/test_google_compute_firewall.py` is the template.

---

## P4-5 remaining rule list (~8 rules)

From the punchlist CG-2 table:

### google_compute_firewall (1 more, helpers already extracted)
* `firewall_no_open_rdp.rego` -- CIS GCP 3.7 (port 3389 instead of 22).
  Mine same template (gcp_restricted_firewall_rules_v1).
* `firewall_logs_enabled.rego` -- CIS GCP 3.x. Mine
  `gcp_network_enable_firewall_logs_v1.yaml`.

### google_compute_disk (2 rules)
* `disk_cmek_required.rego` -- CIS GCP 4.7. Mine
  `gcp_cmek_settings_v1.yaml` (proxy match -- targets the disk's
  diskEncryptionKey.kmsKeyName).
* `disk_snapshot_policy_attached.rego` -- industry consensus.
  Mine `gcp_compute_disk_resource_policies_v1.yaml`.

### google_compute_network (2 rules)
* `network_no_default_vpc.rego` -- CIS GCP 3.1. Mine
  `gcp_network_restrict_default_v1.yaml`.
* `network_routing_mode_regional.rego`. Mine
  `gcp_network_routing_v1.yaml`.

### google_compute_subnetwork (2 rules)
* `subnet_flow_logs_enabled.rego` -- CIS GCP 3.8. Mine
  `gcp_network_enable_flow_logs_v1.yaml`.
* `subnet_private_google_access.rego`. Mine
  `gcp_network_enable_private_google_access_v1.yaml`.

Each rule = ~15 LOC + structural test. One commit covers all 8 +
provenance doc updates.

---

## P4-6 rule list (~11 rules) -- richest mining surface

### google_container_cluster (4 rules from 14 archived templates)
* `cluster_workload_identity.rego` -- CIS GCP 7.x. Mine
  `gcp_gke_enable_workload_identity_v1.yaml`.
* `cluster_private_endpoint.rego`. Mine
  `gcp_gke_enable_private_endpoint.yaml` +
  `gcp_gke_private_cluster_v1.yaml`.
* `cluster_legacy_abac_disabled.rego`. Mine
  `gcp_gke_legacy_abac_v1.yaml`.
* `cluster_master_authorized_networks.rego`. Mine
  `gcp_gke_master_authorized_networks_enabled_v1.yaml`.

### google_container_node_pool (3 rules)
* `node_pool_auto_upgrade.rego` -- CIS GCP 7.x. Mine
  `gcp_gke_node_auto_upgrade_v1.yaml`.
* `node_pool_auto_repair.rego`. Mine
  `gcp_gke_node_auto_repair_v1.yaml`.
* `node_pool_uses_cos.rego`. Mine
  `gcp_gke_container_optimized_os.yaml`.

### google_service_account (2 rules)
* `sa_key_age_max_90_days.rego` -- CIS GCP 1.7. Mine
  `gcp_iam_restrict_service_account_key_age_v1.yaml`. Note:
  Google's archive default is parameterized (no fixed value); CIS
  recommends 90d; we default to 90d (CIS-aligned).
* `sa_no_user_managed_keys.rego` -- CIS GCP 1.4. Mine
  `gcp_iam_restrict_service_account_key_type_v1.yaml`.

### google_kms_crypto_key (2 rules)
* `key_rotation_max_90_days.rego` -- CIS GCP 1.10. Mine
  `gcp_cmek_rotation_v1.yaml`. KEY DETAIL: Google's archive
  default = `31536000s` (1 year); CIS recommends 90 days; we
  default to 90d (stricter). Note both in provenance.
* `key_protection_level_hsm_for_critical.rego`. Mine
  `gcp_cmek_settings_v1.yaml`. Sentinel: `99999999s` for "never
  rotates" -- worth mirroring as a fail-trigger fallback.

---

## P4-7 rule list (~9 rules)

### google_sql_database_instance (3 rules from 7 templates)
* `sql_no_public_ip.rego` -- CIS GCP 6.5. Mine
  `gcp_sql_public_ip_v1.yaml`.
* `sql_ssl_required.rego` -- CIS GCP 6.4. Mine
  `gcp_sql_ssl_v1.yaml`.
* `sql_backup_enabled.rego` -- CIS GCP 6.7. Mine
  `gcp_sql_backup_v1.yaml`.

### google_cloud_run_v2_service (2 rules) -- NO archive
* `cloudrun_no_public_invoker.rego` -- industry consensus +
  Google's current Best Practices docs.
* `cloudrun_min_instances_documented.rego`.

### google_pubsub_topic (2 rules) -- NO archive
* `pubsub_topic_cmek_required.rego` -- CIS Controls v8 3.11.
* `pubsub_topic_iam_no_allusers.rego`.

### google_pubsub_subscription (2 rules) -- NO archive
* `pubsub_sub_dead_letter_configured.rego`.
* `pubsub_sub_iam_no_allusers.rego`.

For Cloud Run + Pub/Sub (no GCP archive): Source line says
`# Source: NONE (Cloud Run/Pub/Sub not covered in archived library)`.
Standard cites CIS Controls v8 + Google Best Practices URL.

---

## P4-8 spec (CG-3 enforcement walker test)

Single test file `policy/tests/test_provenance_enforcement.py`:

```python
def test_every_rule_has_three_source_provenance():
    for rego_path in find_all_rego(exclude=["_helpers.rego"]):
        with open(rego_path) as f:
            contents = f.read()
        for label in ("Source:", "Standard:", "Default:"):
            assert label in contents, f"{rego_path}: missing {label}"

def test_every_rule_has_deny_block():
    ...

def test_every_rule_message_has_severity_rule_id_prefix():
    ...
```

Plus wire metadata into deny[]'s details field for UI
consumption (Phase 6 will render the control IDs).

---

## P4-9a + P4-9b spec (CC-9 golden examples)

P4-9a: scaffolding + 3 priority types.
  * `importer/golden_examples/<tf_type>.tf` directory + loader in
    `importer/hcl_generator.py`
  * Examples for top 3 hallucination-prone types from Phase 2 SMOKE:
      - `google_container_cluster__autopilot.tf` (covers P2-13
        ray_operator_config bug)
      - `google_container_cluster__standard.tf`
      - `google_cloud_run_v2_service.tf` (covers P2-12
        startup_cpu_boost bug)
  * Per-mode specialisation via `<tf_type>__<mode>.tf` filename
  * Loader prepends matching example to system prompt as
    "REFERENCE EXAMPLE"
  * Tests: golden file presence + loader behavior + prompt assembly

P4-9b: remaining 7 examples.
  * `google_container_node_pool.tf`
  * `google_compute_instance.tf`
  * `google_storage_bucket.tf`
  * `google_kms_crypto_key.tf`
  * `google_pubsub_subscription.tf`
  * `google_compute_subnetwork.tf`
  * `google_service_account.tf`

Each example must be plan-clean (`terraform plan` produces no diff
when applied against a real GCP resource of that type).

---

## P4-10 spec (SMOKE 4 + retro)

Manual run by user against `dev-proj-470211`:
  * Importer: 17 types, expect 15-17 imports (CC-9 should lift
    first-attempt rate to ~90%)
  * Translator: AWS + Azure batches via `run_translation_batch()`
  * Detector: drift report + `rescan()` mode finds unmanaged
  * Policy: 16 existing + ~25 new = 41 Rego rules across 17 types

Retro commit captures pass/fail + any new long-tail bugs (queued
for Phase 5 retro or Phase 4 hotfixes).

---

## Quick reference (paths the next session needs)

* **Punchlist source of truth:**
  `docs/saas_readiness_punchlist.md` (CG-2 table at line ~700 has
  the per-type mining map)
* **Provenance matrix:** `docs/policy_provenance.md`
* **Existing 16 regos (mined P4-PRE):** `policy/policies/{common,
  google_compute_instance,google_storage_bucket,aws_instance,
  aws_s3_bucket}/`
* **New P4-5 rule pattern:**
  `policy/policies/google_compute_firewall/firewall_no_open_ssh.rego`
* **Helpers pattern:**
  `policy/policies/google_compute_firewall/_helpers.rego`
* **Test pattern:**
  `policy/tests/test_google_compute_firewall.py`
* **Inventory entry point (P4-2):** `importer/inventory.py`
* **Rescan entry point (P4-3):** `detector/rescan.py`
* **DriftReport (P4-3):** `detector/drift_report.py`
* **Detector scope (P4-4):** `detector/config.py` —
  `IN_SCOPE_TF_TYPES` (17), `DRIFT_AWARE_TF_TYPES` (2)

---

## Open questions for the next session (none blocking)

* CC-1 detector + policy structured-logging migration is still
  pending. Not in any P4-* item explicitly. Could fold into P4-10
  retro or queue as P5 prep.
* The user's instructions.txt.txt has uncommitted changes (`M`
  status) that have been pending across many commits. Worth asking
  the user whether to commit, discard, or ignore.
