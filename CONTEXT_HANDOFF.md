# mtagent — Phase 6 → Phase 7 handoff

**Date:** 2026-05-02
**Branch:** `streamlit-UI`
**Last live revision:** `mtagent-app-00068-qh4` (Cloud Run, project `mtagent-internal-dev`, region `us-central1`)
**Latest commit:** `7e32a46` PUI-4t-fix Recreate copy (committed, NOT built — pending next deploy)

---

## ⭐ NEW client requirement (Phase 7 — to be designed)

The client demo is done. They've requested a **pivot** from "scan live cloud" to "scan IaC repo." The new feature:

### What they want

> Scan our Terraform / Terragrunt GCP templates from a **Git repo** (not live cloud), then convert them to **production-ready AWS HCL** that's a **mirror image** of the GCP templates **with ALL dependencies included** — nothing missing.

### Concretely

1. **Input:** a Git repo URL (GitHub / GitLab / Bitbucket / generic) containing Terraform or Terragrunt configurations targeting GCP.
2. **Discovery:** clone repo → walk all `.tf` / `terragrunt.hcl` / `*.tfvars` / `modules/` / `locals.tf` / `variables.tf` / `outputs.tf` files. Resolve all imports / module references / terragrunt `dependency` blocks.
3. **Translation:** for every GCP resource (`google_*`, `google-beta_*`), generate the equivalent AWS resource (`aws_*`). Include all transitive dependencies — IAM, VPC, subnets, IAM roles, KMS keys, security groups, etc.
4. **Output:** a NEW git-style tree of AWS HCL that:
   - Passes `terraform validate` AND `terraform plan` (production-ready, not just syntactic)
   - Mirrors the GCP file/module structure (matching `main.tf`, `variables.tf`, `outputs.tf`, `terragrunt.hcl` per module)
   - **Has zero missing dependencies** — every `aws_*.foo.attribute` reference resolves
   - Preserves variable interpolations, locals, conditional logic where possible

### Why this is non-trivial vs current Translator

| Today (PUI-3 Cross-Cloud Translation) | New requirement |
|---|---|
| Single `.tf` file at a time | Multi-file repo with modules + terragrunt + tfvars |
| Source = mtagent-imported HCL (clean, single resource per file) | Source = customer-authored production code (messy, modules, locals, conditionals) |
| Output = single file dropped in `gs://.../translated/aws/` | Output = full directory tree mirroring source structure |
| Validates each file standalone | Must validate the WHOLE tree (cross-file references) |
| Per-file LLM call (Phase 1 + Phase 2 + 3 retries) | Need a planner phase — discover deps, build dep graph, translate in topological order |

### Design questions to resolve early

1. **Git auth:** PAT in env var? GitHub App? OAuth flow per customer? SSH key?
2. **Terragrunt:** support both vanilla TF and Terragrunt, or Terragrunt only / TF only?
3. **Modules:** rewrite community/published modules (e.g., `terraform-google-modules/network/google` → `terraform-aws-modules/vpc/aws`) or expect customers to swap themselves?
4. **State migration:** out of scope for v1? (Customer would need to `terraform import` AWS resources separately.)
5. **Output destination:** new Git branch? PR? GCS download? Local zip?
6. **Scale:** how big are customer repos? 100 files? 10k? Affects LLM cost + walltime.
7. **Round-trip cost:** Phase 1 (extract blueprint) + Phase 2 (gen target HCL) + retries × N files = lots of $. Caching strategy?
8. **What "mirror image" means precisely:** field-by-field semantic equivalence? Or "an AWS deployment that achieves the same outcome"? (Often these diverge — e.g., GCP IAM uses bindings, AWS IAM uses policies.)
9. **Resource coverage:** which `google_*` types must we support? (Currently translator covers ~10; customer may need 50+.)
10. **Cluster handling:** GKE → EKS is itself a multi-resource translation (cluster + node groups + IAM service-linked roles + EBS CSI driver + ...). Need ground truth golden examples per cluster.

### Likely architecture (sketch)

```
Phase 1: Git ingestion
  ├── git clone / pull (auth via env var initially)
  ├── walk filesystem, identify .tf / .hcl / .tfvars
  └── parse with hcl2 (already a Python lib) → AST per file

Phase 2: Dependency graph build
  ├── resolve module references (local + remote)
  ├── resolve terragrunt dependencies (`dependency "x" { config_path = "..." }`)
  ├── resolve variable / output / local references across files
  └── build DAG: (resource_address → [referenced_addresses])

Phase 3: Per-resource translation (topological order)
  ├── for each resource in DAG order:
  │     extract blueprint (Phase 1 of current Translator)
  │     generate AWS HCL (Phase 2 of current Translator)
  │     rewrite cross-references to point at AWS equivalents
  └── emit to in-memory tree

Phase 4: Cross-file validation
  ├── write tree to disk
  ├── run `terraform init` (provider download)
  ├── run `terraform validate` → if fail, surface errors per file
  └── run `terraform plan -input=false` → catch dependency miss

Phase 5: Output
  ├── push to new Git branch / open PR / export as zip / write to GCS
  └── render diff summary in UI
```

### Estimated scope

This is a **multi-week** feature, not a sprint. Roughly:
- Phase 1 (Git ingestion): ~3-5 days
- Phase 2 (Dep graph): ~1-2 weeks (the hard part — terragrunt + remote modules)
- Phase 3 (Per-resource translation): reuses existing Translator engine (~3-5 days adapt)
- Phase 4 (Cross-file validation): ~3-5 days
- Phase 5 (Output): ~2-3 days
- UI page + smoke: ~3-5 days

**Total: ~6-8 weeks for a v1 that handles small-to-medium repos.**

### Suggested first conversation in the new chat

> "Read CONTEXT_HANDOFF.md. Then let's discuss the new client requirement (Phase 7 — Git-IaC Translator). Start with the design questions section — I want to nail down (a) Git auth model, (b) Terragrunt vs vanilla TF, (c) what 'production-ready' means for v1 scope, (d) output destination, before we plan implementation."

---

## What's currently live (Phase 6, demo-complete)

### Cloud Run service

- **URL:** `https://mtagent-app-7fddh57yyq-uc.a.run.app` (IAP-gated)
- **Project:** `mtagent-internal-dev`
- **Region:** `us-central1`
- **Service:** `mtagent-app`
- **Latest revision:** `mtagent-app-00068-qh4`
- **Local proxy:** `gcloud run services proxy mtagent-app --region=us-central1` → `http://localhost:8080`

### Sidebar pages

```
🏠 Home                                 (app/🏠_Home.py)
📊 Dashboard                            (app/pages/1_📊_Dashboard.py)
📋 Inventory                            (app/pages/2_📋_Inventory.py)
🌐 Cross Cloud Translation              (app/pages/3_🌐_Cross_Cloud_Translation.py)
🔍 Drift Detection and Remediation      (app/pages/4_🔍_Drift_Detection_and_Remediation.py)
🛡️ Policy as Code                       (app/pages/5_🛡️_Policy_as_Code.py)
```

### Engines (programmatic surfaces)

| Engine | Module | Headless entry | CLI entry |
|--------|--------|----------------|-----------|
| Importer | `importer/` | `importer.run.run_workflow()` | `python -m importer.run` |
| Translator | `translator/` | `translator.run.run_workflow()` | `python -m translator.run` |
| Detector | `detector/` | `detector.rescan.rescan(project_id, project_root, drift_check=True)` | `python -m detector.run` |
| Policy | `policy/` | `policy.scan.scan(project_id, project_root)` | `python -m policy.run` |

### GCS layout (per project, per tenant)

```
gs://mtagent-state-dev/tenants/<tenant>/projects/<project_id>/
├── <google_*_resource_name>.tf          ← codified resources
├── _backend_seed.tf                     ← terraform GCS backend config (PSA-5)
├── _providers_seed.tf                   ← provider stub (D-6 fix)
├── _quarantine/                         ← failed imports + sidecars
├── translated/<aws|azure>/              ← cross-cloud translation outputs
├── snapshots/<engine>/
│   ├── latest.json                      ← envelope: {engine, written_at, tenant_id, project_id, data}
│   └── history/<iso-ts>.json            ← immutable history (PSA-9 + PUI-2pre)
├── terraform-state/default.tfstate      ← GCS terraform backend state
└── .terraform/                          ← provider cache
```

### Critical env vars (set in `cloudbuild.yaml`)

```yaml
GCP_PROJECT_ID                  # bound to $PROJECT_ID
MTAGENT_STATE_BUCKET=mtagent-state-dev
MTAGENT_IMPORT_BASE=/tmp/imported       # per-request workdir base
MTAGENT_PERSIST_BLUEPRINTS=0            # translator's YAML dump off in SaaS
IMPORTER_AUTO_QUARANTINE=1
TRANSLATOR_TARGETS_ALLOWED=aws          # AWS only for client demo (Azure hidden)
MAX_TRANSLATION_WORKERS=4
MTAGENT_LOG_FORMAT=json
MTAGENT_USE_GCS_BACKEND=1               # PSA-5: terraform state to GCS
MTAGENT_PERSIST_SNAPSHOTS=1             # PSA-9 / PUI-2pre: engine snapshots to GCS
TARGET_PROJECT_ID=dev-proj-470211       # PUI-5i: pre-fill sidebar picker
MTAGENT_LOG_LEVEL=INFO
PYTHONUNBUFFERED=1
```

### Critical defenses (don't accidentally regress)

| Code | What it protects |
|------|------------------|
| **PUI-1Q** (`common/storage.py:_PERSIST_EXCLUDES`) | `terraform-state/**` and `.terraform/terraform.tfstate` excluded so persist's delete-orphans loop doesn't wipe terraform state. |
| **PUI-1D** (`common/storage.py:_PERSIST_EXCLUDES`) | `snapshots/**` excluded so persist doesn't wipe Dashboard snapshots. |
| **PUI-1R** (`common/storage.py:hydrate_workdir`) | After hydrate, `.terraform/providers/**/terraform-provider-*` chmod 0o755 so terraform plan/apply can `fork/exec` the binary (GCS doesn't preserve POSIX +x). |
| **PUI-3b** (per-page) | `_cached_at` timestamp on result + auto-recover stale lock when result is newer than lock start (Cloud Run drops idle WebSockets). |
| **PUI-3d** (Translator page) | `@st.cache_data(ttl=30)` on source-files fetch (was a hard session_state cache that never invalidated). |

---

## Recent commit history (most-recent first)

```
7e32a46  PUI-4t-fix  Recreate copy correction (committed, NOT BUILT)
c8f7d31  PUI-4t       Drift Detection terminology fixes
3b9da51  PUI-1S       Inventory pre-flight banner
0c0ba3a  PUI-3d       Translator stale source-files cache fix
748d7dd  PUI-2v       Dashboard donut charts (industry-parity)
52f6c36  chore        gitignore + scratch cleanup
a25183f  PUI-5i       TARGET_PROJECT_ID pre-fill sidebar
5d01890  PUI-5g+5h    Page reorder + strip all Firefly references (30 substitutions)
5321875  PUI-1D       snapshots/** persist exclude (data-loss fix #2)
4d2f62c  PUI-5f       main.py → 🏠_Home.py
72e1d1f  PUI-2        Dashboard page UI (~430 LOC)
f54dc5b  PUI-5e+      "and Remediation" in Drift sidebar label
051ab46  PUI-5e       Emoji prefix in page filenames
322d895  PUI-2pre     Snapshot infrastructure (env var, envelope, list_history,
                       DriftReport orphan/coverage_pct/discovered_by_type)
899199e  PUI-5d       Industry-vocab page renames (Translator→Cross-Cloud, Detector→Drift)
05efd9a  PUI-5b1+5b2  Policy engine programmatic surface + UI page
3022021  PUI-3        Translator + 3a/b/c follow-ups + Inventory auto-recover
0e44e2a  PUI-1F       Importer bundle + PUI-1C Reset + CLI parity defenses
```

---

## Pending todos (carried forward — for the new chat)

### Pending NOW (committed but not deployed)

1. **PUI-4t-fix Recreate copy** — committed `7e32a46`. Whenever the next build fires, it'll deploy.

### Demo-deferred (queued post-demo)

1. **PUI-4u** — Gate remediation buttons by drift type (Recreate only enabled when `drift.error` indicates missing snapshot; Restore/Accept disabled then).
2. **PUI-2-deferred** — tenant_id plumbing parity, Policy CLI/SaaS shape unify, read error distinction.
3. **PUI-4o** — Replace lossy `iam_configuration` ignore on storage_bucket with proper alias rules (so real changes still surface).
4. **PUI-4s-prod** — 2-step destructive confirm dialog on Recreate / Drop for production multi-tenant.
5. **PUI-4q** — Re-expand `DRIFT_AWARE_TF_TYPES` with proper per-type normalization rules (KMS, Pub/Sub, Disk, SA).
6. **PUI-4l** — Regenerate HCL from cloud (engine-new — net-new feature; cloud_snapshot → LLM regen → per-resource HCL writer).
7. **PUI-4n** — Policy gate enforcement on Restore (surface HIGH violations + force operator click-through).
8. **PUI-4d** — Codify-in-Inventory deep-link from Drift Detection Unmanaged tab.
9. **PUI-4f** — Detector polish: coverage-trend graph, severity badges, Export drift report CSV/JSON.

### Stage-2 (pre-customer hardening)

1. **PUI-4h** — Multi-IaC source scanning (Detector treats Git-stored HCL as additional source of truth — DIFFERENT from Phase 7 Translator request, but adjacent).
2. **PUI-1H** — provider-import quirks investigation.
3. **PUI-1J** — operator HCL hint UI.
4. **PUI-1K** — customer override JSON.
5. **PUI-1G** — Persist allowlist (DEMOTED post-PUI-1Q).
6. **PUI-1E** — mutex-pair override.
7. **PERF-T1** — Bake hashicorp/google provider into Docker image (faster cold start).
8. **RUN-LOCK-B** — Tier-B GCS-backed run lock.
9. **End-of-Phase-6 SMOKE 5** — full regression smoke before pre-customer rollout.

### Cluster (long-tail)

1. **PUI-1B FINAL CLUSTER SMOKE** — Both clusters (poc_cluster, poc_cluster_std) STILL quarantined. LLM-1 Claude likely closes this class.
2. **PUI-1P** — Cluster long-tail bugs.
3. **PUI-1M** — `cloud_run_v2_service` deep-dive.

### LLM / arch

1. **LLM-1** — Migrate Gemini → Claude (would close cluster bugs above).
2. **PERF-T4** — DEFERRED — industry-style architectural rewrite.

---

## Key file map (where to look first)

### App layer (Streamlit pages)

```
app/
├── 🏠_Home.py                                          ← entry script (renamed from main.py)
├── pages/
│   ├── 1_📊_Dashboard.py                               ← reads engine snapshots, hero + cards + activity
│   ├── 2_📋_Inventory.py                               ← codify GCP via importer
│   ├── 3_🌐_Cross_Cloud_Translation.py                ← GCP HCL → AWS/Azure (per-file, not multi-file)
│   ├── 4_🔍_Drift_Detection_and_Remediation.py        ← cloud-vs-state diff + 4 remediation actions
│   └── 5_🛡️_Policy_as_Code.py                         ← Rego/conftest evaluation
├── ui/
│   ├── sidebar.py                                      ← project picker (reads TARGET_PROJECT_ID env)
│   ├── theme.py                                        ← dark theme polish + CSS
│   └── error_surface.py                                ← shared error rendering
└── middleware.py                                       ← workdir_context, bust_workdir_cache
```

### Engine layer

```
common/
├── storage.py                       ← hydrate/persist workdir to GCS, _PERSIST_EXCLUDES
├── snapshots.py                     ← write/read/list_history snapshots (PSA-9 + PUI-2pre envelope)
├── workdir.py                       ← per-project workdir resolution
├── logging.py                       ← structlog setup
└── errors.py                        ← PreflightError, etc.

importer/
├── run.py                           ← CLI + run_workflow()
├── inventory.py                     ← Cloud Asset Inventory wrapper
├── terraform_client.py              ← init / state_pull / import_resource / state_rm
├── hcl_generator.py                 ← LLM-driven HCL generation
├── post_llm_overrides.py            ← post-process LLM HCL (renames, deletions, lifecycle)
├── snapshot_scrubber.py             ← cloud snapshot normalization (drops, unwraps)
└── golden_examples/                 ← per-tf_type LLM grounding examples

translator/
├── run.py                           ← CLI + run_workflow()
├── yaml_engine.py                   ← Phase 1 (extract blueprint)
├── target_engine.py                 ← Phase 2 (generate target-cloud HCL)
└── results.py                       ← TranslationResult dataclass

detector/
├── run.py                           ← CLI
├── rescan.py                        ← headless rescan(project_id, project_root, drift_check=True)
├── diff_engine.py                   ← cloud-vs-state per-field diff (the heart of drift detection)
├── cloud_snapshot.py                ← parallel gcloud describe (8-thread pool)
├── state_reader.py                  ← parse terraform.tfstate
├── drift_report.py                  ← DriftReport dataclass + as_fields()
├── remediator.py                    ← _restore / _accept / _recreate / _drop + remediate_one()
└── config.py                        ← IN_SCOPE_TF_TYPES, DRIFT_AWARE_TF_TYPES, alias rules

policy/
├── run.py                           ← CLI
├── scan.py                          ← headless scan(project_id, project_root)
├── policy_report.py                 ← PolicyReport dataclass
├── engine.py                        ← conftest invocation + Violation parsing
└── policies/                        ← vendored Rego rules
```

### Build / deploy

```
cloudbuild.yaml                      ← Cloud Run deploy config (env vars, image tags)
Dockerfile                           ← container build (terraform binary, conftest, Python deps)
scripts/container_entrypoint.sh     ← exec streamlit run app/🏠_Home.py
requirements.txt                     ← pinned Python deps (streamlit==1.56.0, altair==6.1.0, etc.)
```

---

## How to fire a build (in the new chat)

```bash
cd C:/Users/41708/my-terraform-agent
gcloud builds submit --config=cloudbuild.yaml .
```

Takes ~7-9 min. Latest revision auto-deploys to Cloud Run.

To verify:

```bash
gcloud run services describe mtagent-app --region=us-central1 \
  --format="value(status.latestReadyRevisionName)"
```

To check logs:

```bash
gcloud logging read 'resource.type="cloud_run_revision"' --limit=20 --freshness=10m
```

---

## Smoke flow (3-min sanity check after any deploy)

1. Restart proxy → new browser tab → land on latest revision
2. Verify sidebar order + emojis match the list above
3. Project picker auto-pre-fills `dev-proj-470211`
4. Open 📊 Dashboard → cards populate (or "no snapshot yet" if engines weren't re-run)
5. Open 🔍 Drift Detection → click Rescan → wait ~30-60s → verify 6 Compliant + Coverage 24%
6. (Optional) drift the bucket: `gcloud storage buckets update gs://poc-smoke-bucket-dev-proj-470211 --default-storage-class=NEARLINE` → re-Rescan → bucket appears in 🟡 Drift tab → click Restore → verify revert.

---

## Bootstrap prompt for the new chat

> Read `CONTEXT_HANDOFF.md` in the repo root. The Phase 6 SaaS demo is shipped (revision `00068-qh4` on Cloud Run). Now we're starting Phase 7: a Git-IaC Translator that scans customer Terraform/Terragrunt repos targeting GCP and produces production-ready AWS HCL mirroring the input with all dependencies. See the "NEW client requirement" section for full spec + design questions. Let's start by resolving the design questions before any code lands. Don't fire any builds without permission.
