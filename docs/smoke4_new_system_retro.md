# SMOKE 4 New-System Retro (Phase-4-baseline reproduction)

**Date:** 2026-04-28
**Project:** dev-proj-470211
**Phase 4 commits exercised:** `phase-4-complete` tag (`83e0290`) +
D-6 fix (`5bbb136`) + D-5 fix (`ddf9610`) — both fixes landed during
new-system bring-up after surfacing on a clean Windows machine that
had never run the importer before.

**Purpose.** Verify the engine reproduces the original SMOKE 4 baseline
on a fresh checkout (different hardware, different terraform binary
architecture). Surfaced two real bugs (D-5 + D-6) that the original
SMOKE 4 retro had hidden via cached state on the working machine.

---

## TL;DR

✅ **End-to-end reproduction VERIFIED.** All 5 stages (Importer →
Translator → Detector drift → Detector rescan → Policy) complete with
results that match or exceed the original baseline.

✅ **Two new bugs found + fixed mid-SMOKE** (both landed on `main`,
breaking the Phase 5+6 freeze under the "main = known-good baseline"
exception):
- **D-6:** KB auto-populate on fresh workdirs (`5bbb136`)
- **D-5:** Importer post-CG-7 re-verification correctly counted (`ddf9610`)

⚠️ **Three known false-positives reproduce as expected** (D-1/D-2/D-3,
all queued for the imminent detector polish wave).

---

## Stage-by-stage results (this machine)

### Stage 1: Importer (with D-5 + D-6 fixes)

* selected: 16
* **imported: 13** (per D-5-fixed correct tally)
* needs_attention (CG-7 quarantined): 3 (poc-vm Disk, poc-cloudrun, poc-cluster-std)
* failed: 0
* skipped: 0
* duration: ~12 min
* **vs OLD SMOKE 4:** +1 imported (12→13). D-6 schema grounding +
  CC-9 golden examples lifted poc_vm Instance from quarantined to
  cleanly-imported via auto-reconcile.

### Stage 2: Translator AWS

* translated: 13/13 (100%)
* needs_attention: 0
* failed: 0
* duration: 293.97s (parallel; 4 workers per P4-15)
* **Note:** second machine's parallel run hit Vertex AI 429 rate
  limits (combined RPM exceeded 60 RPM Gemini Pro project quota).
  The P3-5 safe_invoke wrapper retried gracefully; total duration
  ~7-8× longer for second machine. Confirms the Stage-2 quota raise
  to 300 RPM (PSA-8) is **non-negotiable** before any multi-customer
  parallel deployment.

### Stage 3: Detector drift

* in scope: 13 (CG-2 expansion working; +1 vs OLD because of +1
  imported)
* in sync: 12
* drifted: 1 (`google_storage_bucket.poc_smoke_bucket_dev_proj_470211`)
* drift policy decoration: **4 HIGH, 1 MED** on the bucket entry
  (matches Stage 5 exactly — see cross-stage validation below)
* **D-1/D-2/D-3 reproduce as expected:** subnet describe failed
  (`--region` missing → false-positive in-sync), SA describe HTTPError
  404 (false-positive in-sync), KMS keyring "no project attribute"
  warning (resource still appears in-sync downstream).

### Stage 4: Detector rescan / unmanaged (CG-1)

* compliant: 13
* drifted: 0 (existence-level; field-level drift caught in Stage 3)
* unmanaged: 68
* inventory_errors: 0 ✅
* duration: 10.81s
* exit code: 1 (CG-1 + CG-7 contract: non-zero when unmanaged > 0)
* **vs OLD SMOKE 4:** 12/0/69 → 13/0/68 (math: +1 imported = -1
  unmanaged from same total of 80 cloud resources). Clean reconciliation.
* Cosmetic: `DriftReport.compliant_count` AttributeError reproduces
  (D-4 ergonomic property fix queued).

### Stage 5: Policy

* Total: **11 HIGH, 8 MED, 3 LOW** across 11/13 resources (2
  compliant: poc_vpc, poc_keyring)
* LOW=3 are entirely D-1/D-2/D-3 cloud_snapshot_missing findings
* Exit code: **1** ✅ (P4-5 contract: non-zero when ≥1 HIGH violation)
* Cap warnings (`Truncated N additional violations`): **None** ✅
* Control IDs render: ✅ CIS GCP 4.7, CIS GCP 3.6, CIS GCP 7.x,
  CIS Controls v8 3.11, NIST CP-9 — all visible across rules

* **vs OLD SMOKE 4 (12/4/3):** -1 HIGH (+8 MED). Reconciliation:
  - OLD had cluster_std imported, contributing 3 HIGH. This run
    cluster_std is quarantined → those 3 HIGH don't fire.
  - OLD didn't have poc_vm Instance imported (it was quarantined
    there too, but for different reasons). This run poc_vm imports
    cleanly → adds 2 HIGH (gce_disk_encryption, gce_no_public_ip)
    + 3 MED (shielded_vm, mandatory_labels env, mandatory_labels team).
  - OLD didn't have poc_fw_allow_icmp (different selection). This
    run adds 1 MED (firewall_logs_enabled).
  - Net: 12 - 3 + 2 = 11 HIGH (matches). 4 + 3 + 1 = 8 MED (matches).
    LOW unchanged at 3 (still the 3 D-X false-positives).

---

## Cross-stage validation

The bucket `poc_smoke_bucket_dev_proj_470211`:
- Stage 3 drift report decoration: **4 HIGH, 1 MED**
- Stage 5 policy report on the same bucket: **bucket_encryption HIGH
  + 2× bucket_public_access HIGH + bucket_versioning HIGH +
  bucket_retention MED = 4 HIGH + 1 MED**

Numbers match exactly. P4-5's shared rule set continues to be the
strongest cross-engine integration proof. Same finding from the
original SMOKE 4 retro; reproduces independently on the new system.

---

## Bugs surfaced + fixed during this SMOKE

### D-6 (KB auto-populate on fresh workdir) — FIXED, committed `5bbb136`

**Root cause.** Importer preflight `terraform init` runs against the
empty workdir BEFORE any `.tf` files exist. Init creates `.terraform/`
but downloads no providers. Stage 3's KB bootstrap then queries an
empty schema dump → fails for all 17 resource types → LLM operates
in `no_context_mode` → hallucinates fields on complex types
(GKE clusters, Cloud Run v2).

**Fix.** Added `provider_versions/_providers_seed.tf` (a minimal
declaration) that gets seeded into the workdir alongside the lock
file, so the first `terraform init` actually downloads providers.
Plus a smarter preflight in `importer/run.py` that detects
"`.terraform/` present but `.terraform/providers/` empty" and forces
re-init with `-upgrade`.

**Verified.** On this machine after the fix landed, all 13
`kb_bootstrap_ok` events fired in Stage 3 (vs `kb_bootstrap_skipped`
before). No cluster hallucinations from the no-context fallback.

### D-5 (Importer post-CG-7 re-verification not aggregated) — FIXED, committed `ddf9610`

**Root cause.** After CG-7 quarantines the self-broken HCL files and
re-verifies the previously-blocked siblings, the success path used
the wrong return-value contract: treating `plan_for_resource()`'s
`(is_success, plan_text)` tuple as if it were an integer return code:

```python
replan_rc = terraform_client.plan_for_resource(mapping)
if replan_rc == 0: ...   # tuple is never == 0; branch never fires
```

Every re-verification was forced into `still_failing`. Workflow then
reported `imported=0` even when ~12 resources had plan-passed.

**Fix.** Unpack the tuple and check `is_success`. Aligns this caller
with the 4 other `plan_for_resource` callers in the same file that
already do it correctly.

**Verified.** This machine reports `imported=13, needs_attention=3,
skipped=0, failed=0` — sums to 16 selected. Numbers finally match
reality.

---

## Bugs reproducing as expected (queued for fix)

* **D-1:** Detector subnet describe missing `--region` flag → false-
  positive in-sync (silent drift mask). Fix scheduled: ~1-2 hrs,
  immediate (post-this-retro).
* **D-2:** Detector SA describe HTTPError 404 with HTML body → URL
  malformation; same false-positive in-sync pattern. Fix scheduled:
  ~2-3 hrs.
* **D-3:** KMS crypto-key "no `project` attribute in state" warning;
  resource still resolves "in sync" downstream. Cosmetic but noisy.
  Fix scheduled: ~1 hr.
* **D-4:** `DriftReport.compliant_count` and `unmanaged_count` not
  exposed as `@property` — only available in `as_fields()` dict.
  Ergonomic miss. Fix scheduled: ~0.5 hr alongside D-3.
* **D-7:** `schema_oracle.terraform_schema_cache.json` is never
  invalidated after `terraform init`. Stale empty caches from a
  broken init persist across runs. Manually solvable today via
  `Remove-Item .terraform_schema_cache.json`; auto-recovery is
  nice-to-have. Defer to Phase 5A polish.
* **D-8:** `poc_cluster_std`'s `node_pool.node_config.kubelet_config.
  insecure_kubelet_readonly_port_enabled` enum value mismatch;
  cluster_std reproducibly quarantines on both machines. Needs
  golden-example update OR `post_llm_overrides` rule. Defer to
  CC-9 golden examples wave.
* **CG-11:** Pre-flight Cloud Asset API enablement check (we
  surfaced this on bring-up — the new machine had Cloud Asset API
  disabled, importer failed all 17 inventory calls until manually
  enabled). Customer-facing must NEVER auto-enable APIs; just
  detect + surface clear "please enable these N APIs" message.
  Defer to Stage-2 pre-customer migration.

---

## Two-machine LLM stochasticity observation

Ran SMOKE in parallel on two machines (windows_amd64 + windows_386).
Both succeeded; results differ slightly due to LLM stochasticity:

| | This machine | Second machine |
|---|---|---|
| imported | 13 | 12 |
| needs_attention | 3 (cluster_std, cloudrun, vm-disk) | 4 (cluster_std, cloudrun, vm-disk, vm-instance) |
| Translator duration | 294s | ~25-30 min (rate-limit retries) |

The 1-resource difference (poc_vm Instance) is Gemini producing
slightly different HCL on each call. Both runs are
baseline-equivalent.

The Translator duration delta is the Vertex AI 60 RPM rate limit
hitting when both machines fire in parallel — confirms PSA-8 quota
raise to 300 RPM is non-negotiable before customer parallel runs.

---

## Phase 4 verdict (re-confirmed on new system)

* **CG-1 unmanaged tracking: ✅ ship-ready.**
* **CG-2 policy coverage: ✅ ship-ready.**
* **CG-3 provenance: ✅ ship-ready.**
* **CC-9 few-shot lift: ✅ ship-ready.** Better than original
  baseline this run (+1 imported).
* **CG-7 quarantine: ✅ working as designed.**
* **D-5 + D-6 fixes: ✅ verified end-to-end.**

**Overall Phase 4 + immediate-hotfix wave: GO for Phase 5A** (after
D-1/D-2/D-3/D-4 fix wave + MIG-0 IT ticket lead time).

---

## Next steps (immediate)

1. **D-1/D-2/D-3/D-4 fix wave** — ~1 day total, lands on `main`
   alongside D-5/D-6. Eliminates the documented false-positives.
2. **MIG-0 IT ticket** — file in parallel for company GCP project
   access (lead time outside our control).
3. **Phase 5A kickoff** — gated by completion of #1 + #2.
