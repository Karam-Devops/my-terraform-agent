# Phase 5 + 6 Plan — SaaS Round-1 (CG-8H + Firefly-Parity UI)

**Drafted:** 2026-04-28 (post-SMOKE-4 retro / `83e0290` / `phase-4-complete`)
**Target:** Round-1 customer demo-ready SaaS deployment
**Total estimate:** ~10-11 working days (Phase 5A: 3d + Phase 6: 7-8d)
**Phase 5A start:** post-sign-off
**Round-1 customer cohort:** 1 customer, no live hotfixes during their test window

**Two-stage hosting model (per user decision 2026-04-28):**

| Stage | Hosting GCP project | Target GCP project | Auth | Vertex AI quota |
|---|---|---|---|---|
| **Internal dev (Phase 5A + 6 build)** | `mtagent-internal-dev` (new, this plan creates it) | `dev-proj-470211` (existing SMOKE 4 baseline) | Single-user IAM allowlist (owner email only) | Default (60 RPM Gemini Pro) — sufficient for solo testing |
| **Pre-customer migration (Phase 6.5)** | Company GCP project (TBD) | Customer GCP project (TBD) | IAP + allowlist of customer's Google accounts | 300 RPM raise (1-3d GCP approval lead) |

The architecture is identical between stages — only project names + auth + quota change. PSA-6 cross-project SA impersonation + GCS prefixing are designed to make the migration a config-only change (no code edits).

**Branching strategy (per user decision 2026-04-28):**

* `main` — phase plan + Phase 5A plumbing commits (PSA-X items)
* `streamlit-UI` — all Phase 6 UI commits (PUI-X items); branched from `main` after this plan lands. Merged back to `main` at end of Phase 6.

---

## 1. Goals & Non-Goals

### Goals (must ship for Round-1)

| # | Goal | Why it matters |
|---|---|---|
| G1 | Single-URL Cloud Run deployment customer can hit from a browser | Replaces "git clone + python -m" CLI |
| G2 | Cross-session state persistence via GCS (CG-8H) | Customer can return tomorrow and see yesterday's import |
| G3 | Firefly-style Inventory page as the primary UI surface | Vendor-evaluator immediate recognition (≤30s "I get it" test) |
| G4 | Dashboard home with IaC coverage % + violation counts | Sets customer mental model in 5 seconds |
| G5 | Side-by-side drift diff + downloadable fix patch | Demo-able remediation story without OAuth scope creep |
| G6 | AI policy remediation (LLM rewrites resource to satisfy rule) | Differentiator vs. Firefly's per-violation flow (we batch) |
| G7 | Per-resource progress bars during Importer / Translator runs | Without this, app feels broken on long runs |
| G8 | Cached inventory + manual "Rescan now" button | Mandatory: every page-load shouldn't burn GCP API quota |
| G9 | AWS-only target dropdown (TRANSLATOR_TARGETS_ALLOWED=aws) | CG-6 Round-1 scoping; Azure capability preserved in CLI |
| G10 | IAP authentication on the Cloud Run URL | Real-but-simple auth without Identity Platform complexity |
| G11 | Customer onboarding doc (5-step runbook) | Zero-friction project ID + IAM binding exchange |

### Non-goals (explicit deferrals — Phase 5B / Phase 6+ / Round-2)

- ❌ **Connect customer's existing Git repo as IaC source** (Q1 follow-up). Round-1 = "we built you a fresh inventory."
- ❌ **GitHub/GitLab PR creation flow** (Q2 / Q4 OAuth flow). Round-1 = "Download patch" button. PR flow defers to Round-2 once a customer asks.
- ❌ **Live `terraform apply` from UI** (Q3 Option B). Round-1 read-only; customer applies via their own pipeline.
- ❌ **Editable `.tf` file viewer** (Q5 Option B). Read-only `st.code()` only.
- ❌ **Background scheduled rescans** (Q8 Cloud Scheduler integration). Round-1 = manual refresh; cadence dropdown is a placeholder.
- ❌ **Firestore for per-resource metadata** (CG-9 / Phase 6+). GCS JSON snapshots are sufficient for Round-1.
- ❌ **Multi-region Cloud Run** (CG-9 / Phase 6+). Single region (`us-central1`) co-located with Vertex AI.
- ❌ **Cloud Tasks for long-running jobs** (CG-9 / Phase 6+). Round-1 uses Cloud Run's 60-min request timeout + a polling pattern.
- ❌ **Multi-tenant in a single deploy** (CG-9 / Phase 6+). Round-1 is single-tenant per Cloud Run service. The GCS prefixing IS multi-tenant-ready, but the auth layer isn't.

---

## 2. Architecture (Round-1 deployment shape)

```
┌────────────────────────────────────────────────────────────────┐
│                  Customer's GCP project                        │
│  ┌──────────────────┐                                          │
│  │  Cloud assets    │ ◄── gcloud asset search (read-only)      │
│  │  (the inventory) │                                          │
│  └──────────────────┘                                          │
│  ┌──────────────────┐                                          │
│  │  IAM binding:    │ ◄── grants impersonation to our SA       │
│  │  mtagent-sa@     │                                          │
│  └──────────────────┘                                          │
└──────────┬─────────────────────────────────────────────────────┘
           │ SA impersonation (no key exchange)
           ▼
┌────────────────────────────────────────────────────────────────┐
│        Our hosting GCP project                                 │
│        (Stage 1: mtagent-internal-dev / Stage 2: company proj) │
│                                                                │
│  ┌─────────────────────────────────────────────────────────┐   │
│  │ Cloud Run service: mtagent-app                          │   │
│  │   image: gcr.io/<host-project>/mtagent-app:<sha>        │   │
│  │   memory=8Gi cpu=4 max=3 timeout=3600s                  │   │
│  │   env: TRANSLATOR_TARGETS_ALLOWED=aws                   │   │
│  │        MTAGENT_PERSIST_BLUEPRINTS=0                     │   │
│  │        IMPORTER_AUTO_QUARANTINE=1                       │   │
│  │        MAX_TRANSLATION_WORKERS=8                        │   │
│  │        MTAGENT_IMPORT_BASE=/tmp/imported                │   │
│  │   /tmp scoped per-request: /tmp/imported/<request_uuid>/│   │
│  │   Streamlit serves on :8080                             │   │
│  └────────────────┬─────────────────────┬────────────────-─┘   │
│                   │                     │                      │
│                   ▼                     ▼                      │
│           ┌───────────────┐    ┌────────────────┐              │
│           │ Auth          │    │  Vertex AI     │              │
│           │ Stage 1: IAM  │    │  Stage 1: 60 RPM (default)    │
│           │  owner-only   │    │  Stage 2: 300 RPM (raise req'd)│
│           │ Stage 2: IAP  │    │  (Gemini 2.5)  │              │
│           │  + allowlist  │    │                │              │
│           └───────────────┘    └────────────────┘              │
│                                                                │
│  ┌─────────────────────────────────────────────────────────┐   │
│  │ GCS bucket                                              │   │
│  │   Stage 1: mtagent-state-dev                            │   │
│  │   Stage 2: <company-naming>-state-prod                  │   │
│  │   versioning: ON                                        │   │
│  │   lifecycle: archive @ 90d                              │   │
│  │   bucket lock: immutable audit history                  │   │
│  │   layout:                                               │   │
│  │     tenants/<tenant>/projects/<project>/imported/...    │   │
│  │     tenants/<tenant>/projects/<project>/translated/...  │   │
│  │     tenants/<tenant>/projects/<project>/state/...       │   │
│  │     tenants/<tenant>/projects/<project>/snapshots/...   │   │
│  └─────────────────────────────────────────────────────────┘   │
└────────────────────────────────────────────────────────────────┘
            ▲
            │ HTTPS via IAP-protected Cloud Run URL
            │
       ┌─────────┐
       │Customer │
       │ browser │
       └─────────┘
```

**Per-request lifecycle (Cloud Run handler pseudocode):**
```
1. Request arrives at Cloud Run (IAP-validated; tenant_id derived from IAP token).
2. Generate request_uuid; mkdir /tmp/imported/<request_uuid>/.
3. common.storage.hydrate_workdir(tenant_id, project_id) →
   gcloud storage rsync gs://...prefix.../  /tmp/imported/<request_uuid>/
4. Engine code runs against /tmp/imported/<request_uuid>/ (no code changes;
   MTAGENT_IMPORT_BASE handles the path).
5. common.storage.persist_workdir(local, tenant_id, project_id) →
   gcloud storage rsync /tmp/.../ gs://...prefix.../
   (with --exclude=_diagnostics/** --exclude=*.backup)
6. Return response; rm -rf /tmp/imported/<request_uuid>/.
```

---

## 3. Workstream Catalog

Two ID prefixes for trackability (matching P4-X convention):
- **PSA-X** = Phase 5A items (deployment plumbing)
- **PUI-X** = Phase 6 items (UI / UX)

### Phase 5A workstreams (deployment plumbing — 3 days)

| ID | Workstream | Effort | Depends on | Exit criterion |
|---|---|---|---|---|
| PSA-1 | Dockerfile (Python 3.11 + terraform + gcloud + provider plugins pre-baked) | 0.5d | none | `docker build .` produces image; `docker run` boots Streamlit on :8080 |
| PSA-2 | `cloudbuild.yaml` (build + push to GCR + deploy to Cloud Run) | 0.25d | PSA-1 | `gcloud builds submit` builds & deploys; URL responds with HTTP 200 (behind IAP) |
| PSA-3 | `common/storage.py` (hydrate_workdir + persist_workdir helpers) | 0.5d | none | Unit tests pass with mocked subprocess.run; integration test with real GCS bucket round-trips a workdir |
| PSA-4 | Per-request `/tmp/<request_uuid>/` scoping middleware | 0.25d | PSA-3 | Two simulated parallel requests on same instance produce separate workdirs; both clean up on exit |
| PSA-5 | Terraform GCS backend wiring in generated `.tf` files | 0.25d | none | Generated importer output includes `terraform { backend "gcs" { ... } }`; `terraform init` succeeds against test bucket |
| PSA-6 | Cross-project SA impersonation setup + onboarding doc (template, used in Stage 1 against `dev-proj-470211` + Stage 2 against customer project) | 0.5d | none | Doc walks through 5 steps; SA impersonation works end-to-end against `dev-proj-470211` |
| PSA-7 | **(Stage 1)** Cloud Run IAM binding restricting invoker role to owner email only | 0.1d | PSA-2 | Only owner email can invoke Cloud Run URL; other Google accounts get 403 |
| PSA-9 | Dashboard / Inventory result-persistence layer (writes WorkflowResult / DriftReport / PolicyReport JSON to GCS after each engine run) | 0.5d | PSA-3 | Engine completion writes `gs://.../snapshots/latest.json`; helper `read_latest_snapshot()` returns a typed view |

**PSA total: ~3 days.** PSA-9 is the bridge to Phase 6 (Q7 + Q8 caching depend on it).

**Deferred to Stage 2 (pre-customer migration):**

| ID | Workstream | Effort | When |
|---|---|---|---|
| PSA-7b | IAP front-end (OAuth client + allowlist + Cloud Run binding) | 0.25d | Pre-customer migration. Replaces the Stage-1 IAM-only binding. |
| PSA-8 | Vertex AI quota raise to 300 RPM | 0.25d work + 1-3d GCP approval lead | Pre-customer migration. Internal solo testing fits in default 60 RPM. Submit request when company hosting project is created. |

These two items move out of the 3-day Phase 5A budget into the eventual "Stage 2 migration" wave (estimated 0.5-1 day total, mostly waiting on GCP).

### Phase 6 workstreams (UI / UX — 7-8 days)

| ID | Workstream | Effort | Depends on | Q-mapping | Exit criterion |
|---|---|---|---|---|---|
| PUI-1 | Streamlit app skeleton (multi-page; left rail nav: Dashboard / Inventory / Codify / Drift / Policy / Settings; Firefly dark navy palette) | 1d | none | UI shell | All 6 pages route; left-rail nav highlights active; dark theme applied via `.streamlit/config.toml` |
| PUI-2 | **Dashboard home page** (4 hero metric cards + IaC Status donut + recent activity list + top-5 violations table) | 1.5d | PSA-9 | Q7 | Loads in <2s from cached snapshot; numbers cross-check against engine outputs |
| PUI-3 | **Inventory page** (Firefly-parity grid: Cloud / Type / Name / Env / IaC Status badge / Flags / Location; filter chips; click-row drawer) | 2d | PSA-9, PUI-1 | Q5, Q6, Q8 | All 5 IaC Status values render; clicking a row opens drawer with Code / Drift / Policies tabs |
| PUI-4 | **Cached snapshot read + manual "Rescan now" button** (timestamp display, polling spinner during rescan) | 0.5d | PSA-9, PUI-3 | Q8 | Page shows "Last scanned: Xm ago"; "Rescan now" triggers backend call + UI refresh |
| PUI-5 | **`.tf` file viewer in drawer Code tab** (`st.code(content, language="hcl")` + `st.download_button`) | 0.5d | PUI-3 | Q5 | Any imported file renders syntax-highlighted; download button serves raw bytes |
| PUI-6 | **Side-by-side drift diff in drawer Drift tab** + "Generate Fix" button (LLM rewrites resource → shows diff → download) | 1.5d | PUI-3 | Q3 | Drift entry renders cloud vs. state; Generate Fix produces a downloadable `.tf` patch |
| PUI-7 | **AI policy remediation in drawer Policies tab** (per-violation Fix button + batch "Fix all HIGH" button) | 1d | PUI-3 | Q4 | Each violation has a Fix button; per-violation LLM call returns a diff; batch mode rewrites whole resource |
| PUI-8 | **Per-resource progress bars** (refactor `run_import_pipeline` + `run_translation_batch` to yield events; bind to `st.write_stream`) | 1.5d | none | Q6 | Progress bar updates per-resource during a 10-file import; live log entries render as they happen |
| PUI-9 | Codify page (multi-select unmanaged resources → "Run Importer on selected") | 0.5d | PUI-3 | derived | Selected resources flow into existing importer batch path |
| PUI-10 | Settings page (placeholder for cadence dropdown + tenant info display) | 0.25d | PUI-1 | Q8 partial | Page renders; dropdown is non-functional placeholder for Phase 5B |

**PUI total: ~10.25 days raw; with overlap (PUI-2/3/4 share dashboard data; PUI-5/6/7 share drawer) ~7-8 days actual.**

---

## 4. Sequencing & Milestones

```
Day 1   PSA-1 Dockerfile + PSA-3 common/storage.py
        └─ Milestone M1: container builds + GCS round-trip works locally

Day 2   PSA-2 cloudbuild + PSA-4 /tmp scoping + PSA-5 GCS backend
        └─ Milestone M2: Cloud Run instance up; /tmp scoping verified

Day 3   PSA-6 SA impersonation doc + PSA-7 owner-IAM binding + PSA-9 snapshot persistence
        └─ Milestone M3: Phase 5A done. SaaS shell deployed against
           mtagent-internal-dev hosting + dev-proj-470211 target.
           CLI customer journey works end-to-end through the Cloud
           Run URL (owner-only access).

────────── Phase 5A end / Phase 6 start ──────────

Day 4-5 PUI-1 app skeleton + PUI-2 dashboard
        └─ Milestone M4: customer's first impression — dashboard renders with real numbers

Day 6-7 PUI-3 inventory grid + PUI-4 cached snapshot/rescan + PUI-5 file viewer
        └─ Milestone M5: customer can browse all their resources, click into any one

Day 8-9 PUI-6 drift diff + Generate Fix + PUI-7 AI remediation
        └─ Milestone M6: remediation story complete (download patches for drift + violations)

Day 10  PUI-8 progress bars + PUI-9 Codify page + PUI-10 Settings
        └─ Milestone M7: Round-1 demo-ready

Day 11  Buffer / smoke test (SMOKE 5 — full SaaS end-to-end run as customer would)
        └─ Milestone M8: GO/NO-GO decision for customer onboarding
```

---

## 5. Acceptance Criteria — "Round-1 Demo-Ready"

A demo run that proves Round-1 readiness, executed by an Anthropic engineer pretending to be a brand-new customer:

1. Open a fresh browser, navigate to the Cloud Run URL.
2. IAP login screen appears; log in with allowlisted Google account.
3. **Dashboard renders within 2s** showing: 0 assets, 0% IaC coverage, 0 violations (clean-slate state).
4. Click "Codify" → enter customer GCP project ID → "Run Importer".
5. **Per-resource progress bar advances** through ~12 resources over ~3 minutes.
6. Importer completes: dashboard updates to show 12 assets, IaC coverage %, etc.
7. Click "Inventory": all 12 codified resources visible in the grid with green Codified badges.
8. Click any row → drawer opens with Code tab (syntax-highlighted `.tf`).
9. Click "Translate" tab in drawer → AWS HCL renders; download button works.
10. Force a drift (out-of-band cloud change), click "Rescan now" on Inventory.
11. **Last-scanned timestamp updates**; the drifted row shifts to amber Drifted badge.
12. Click drifted row → Drift tab → side-by-side diff renders → "Generate Fix" → downloadable patch.
13. Click Policy tab on any high-violation resource → per-violation Fix buttons → batch "Fix all HIGH" produces a single rewritten resource.
14. Close the browser, return next day, open the URL again → **all state preserved** (CG-8H GCS persistence working).

**Pass criteria:** all 14 steps complete in a single ~15-minute session without engineer intervention. Any failure = NOT demo-ready.

---

## 6. Risks & Mitigations

| # | Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|---|
| R1 | Cloud Run cold-start latency >15s makes first-request feel broken | Med | Med | Min instances=1 (CG-8H already specs this implicitly); pre-warm via PSA-7 health check |
| R2 | `gcloud storage rsync` slow on workdirs >100MB | Low | Med | Round-1 customer projects unlikely to exceed; if hit, switch to `gsutil -m cp -r` parallel |
| R3 | IAP customer-onboarding friction (Google account ↔ allowlist) | High | Low | PSA-6 doc explicitly walks through this; 5-step runbook with screenshots |
| R4 | Vertex AI 300 RPM quota raise rejected by GCP | Low | High | PSA-8 starts day 1; if rejected, MAX_TRANSLATION_WORKERS=4 fallback (still works, slower) |
| R5 | Streamlit `st.write_stream` doesn't compose well with the engine refactor (PUI-8) | Med | Med | Fallback: PUI-8 uses log-tailing pattern (~0.5d less clean); engine refactor still ships but UI binds to logs |
| R6 | LLM remediation (PUI-6 / PUI-7) generates invalid HCL the customer downloads | Med | Med | Run `terraform validate` server-side on the generated patch before offering download; surface validation errors as "we couldn't auto-fix this — see manual remediation guide" |
| R7 | Customer has existing Terraform repo expecting different state-backend layout | High | Med | Onboarding doc explicit: "Round-1 manages state in OUR GCS bucket. Round-2 will offer state-backend bridging." Customer accepts upfront. |
| R8 | A SMOKE-4 D-1/D-2/D-3 detector bug fires during the customer demo | Med | Low | Pre-flight: run SMOKE 5 against a fresh dev project before each customer demo; D-1/D-2/D-3 surface as LOW snapshot-missing findings, NOT crashes — degraded but not broken |

---

## 7. Out-of-Scope (Phase 5B / Phase 6+ / Round-2)

Pinned here so we don't scope-creep mid-build:

- **Phase 5B (CG-9 stack):** Firestore for per-resource metadata, multi-region Cloud Run, Cloud Tasks for long-running jobs, Identity Platform (real auth beyond IAP), background scheduled rescans (Cloud Scheduler).
- **Round-2 customer-driven:** GitHub OAuth + PR creation flow (Q2/Q4), Connect customer's existing Git/state-backend (Q1), Editable `.tf` viewer with save-back (Q5 Option B), Live `terraform apply` from UI (Q3 Option B).
- **Detector polish (D-1..D-4 from SMOKE 4 retro):** Could ship inline during Phase 5A if convenient, but not a gate. Each is a 1-2 hour fix.
- **CG-10 Inventory auto-created defaults filter:** 0.5d add inside PUI-3 if there's slack; otherwise queue for Phase 6.1.

---

## 8. SMOKE 5 (post-Phase-6)

After Day 10 of the plan, run the full Round-1 demo flow (Section 5's 14 steps) against a fresh dev project. Same retro-doc structure as `docs/smoke4_retro.md`. Failures → hotfix wave → re-run. Pass → green-light customer onboarding.

---

## 9. Decisions confirmed (user 2026-04-28)

| # | Question | Decision |
|---|---|---|
| 1 | Hosting GCP project | **Create `mtagent-internal-dev`** for Stage 1. Migrate to company GCP project for Stage 2 (pre-customer). PSA-1 includes API enablement script (Cloud Run, Cloud Build, GCR / Artifact Registry, IAM, Vertex AI, IAP, Cloud Storage). |
| 2 | GCS bucket name | **`mtagent-state-dev`** in `mtagent-internal-dev`. Renamed at Stage-2 migration to company convention. |
| 3 | IAP allowlist | **Deferred to Stage 2.** Stage 1 uses Cloud Run IAM binding restricting invoker to owner email only — sufficient for solo internal testing. |
| 4 | Round-1 customer GCP project | **Not yet identified.** Use existing `dev-proj-470211` as the target for all Stage 1 testing (preserves SMOKE 4 baseline + zero new IAM setup). |
| 5 | Vertex AI quota raise | **Deferred to Stage 2.** Solo testing peak load (~5 RPM) sits well inside default 60 RPM Gemini Pro per-project quota. Quota raise paired with Stage-2 migration so 1-3d GCP approval lead doesn't block Phase 5A. |
| 6 | Streamlit theming / dependencies | **Stack: `streamlit-aggrid` + `streamlit-echarts` + `streamlit-extras`** (3 add-ons). Rationale below. |

### Q6 deep-dive: chosen Streamlit dependency stack

For Firefly visual parity, vanilla Streamlit alone won't satisfy CG-6 (the
default `st.dataframe` doesn't support row-click events, sticky headers,
filter chips, or status-badge cells the way Firefly's grid does). Adding
three vetted community libraries closes the gap without dragging in a
React bundle:

* **`streamlit-aggrid`** — wraps AG Grid (the same enterprise data-grid
  used by Datadog / Asana). Gives us:
    - Sticky-header sortable columns
    - Row click → fires Python callback (powers PUI-3 drawer flow)
    - Filter chips above grid (powers CG-4 IaC Status filtering)
    - Status badge custom cell renderer (powers CG-4 + CG-5 visual parity)
    - Dark theme support out of the box

* **`streamlit-echarts`** — Apache ECharts wrapper. Gives us:
    - Donut chart for IaC Status breakdown on Dashboard
    - Optional gauge for IaC coverage %
    - Visually identical aesthetic to Firefly's chart style

* **`streamlit-extras`** — utility belt. Gives us:
    - `colored_header` (powers section headers)
    - `metric_cards` (richer than `st.metric` — closer to Firefly hero cards)
    - `tags` (powers Flags column chips)
    - `add_vertical_space`, `mention`, etc.

**What we explicitly skip:**
* `streamlit-elements` (Material UI wrapper) — adds 3-4MB React bundle
  + slower cold start. Overkill for Round-1; revisit only if Round-2
  customer feedback demands smoother animations.
* `streamlit-shadcn-ui` — newer / less battle-tested.

**The one Firefly visual we won't match in Round-1:** smooth right-slide
drawer animation. PUI-3 uses a 2-column layout (list left / details
right) instead — functionally identical, just no slide. Can swap to a
real animated drawer in Round-2 (~1 day with `streamlit-elements`) if
customer feedback demands it.

---

## 10. Phase 5A kickoff checklist

When you say "go," I will:

1. Phase 5A (PSA-X plumbing) commits land on **`main`** (deployment scaffolding isn't UI work).
2. Phase 6 (PUI-X UI) commits land on **`streamlit-UI`** branch (created from main after this plan + Phase 5A complete).
3. Start with PSA-1 (Dockerfile) + PSA-3 (`common/storage.py`) in parallel — no dependency between them.
4. PSA-1 first commit includes the GCP project bootstrap script (creates `mtagent-internal-dev`, enables Cloud Run + Cloud Build + GCR + IAM + Vertex AI + Cloud Storage APIs, creates the `mtagent-state-dev` GCS bucket).
5. NO Vertex AI quota request submission this phase — deferred to Stage 2.
6. Daily progress updates against the milestones in Section 4.

## 11. Stage-2 migration (pre-customer onboarding)

When you're ready to onboard the first customer, the migration tasks
(parked here so they don't get lost):

| ID | Task | Effort | Lead time |
|---|---|---|---|
| PSA-7b | Replace owner-IAM binding with IAP front-end + customer-account allowlist | 0.25d | none |
| PSA-8 | Submit Vertex AI quota raise to 300 RPM | 0.25d work | **1-3d GCP approval** — submit early |
| MIG-1 | Create company GCP project (or use existing) + enable APIs (mirrors PSA-1 bootstrap) | 0.5d | depends on company GCP admin |
| MIG-2 | Create company GCS bucket; rename `mtagent-state-dev` references to company convention | 0.25d | none |
| MIG-3 | Update Cloud Run env vars for company project (image registry, GCS bucket name, hosting project ID) | 0.25d | none |
| MIG-4 | Onboarding handoff: customer creates SA impersonation binding to our SA in their project | 0.25d work + customer time | depends on customer |
| MIG-5 | SMOKE 6: full Round-1 demo flow (Section 5's 14 steps) against customer's actual project | 0.5d | none |

**Stage-2 total: ~2 days of our work + 1-3d GCP quota lead + customer's own onboarding time.**

The architecture is identical to Stage 1 — only project IDs, GCS bucket name, and auth layer change. PSA-6's onboarding doc template is the artifact MIG-4 reuses (just swap `dev-proj-470211` for the customer's project ID).
