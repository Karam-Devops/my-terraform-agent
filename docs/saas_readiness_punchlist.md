# SaaS Readiness Punchlist (Phase 0 synthesis)

**Source.** Four parallel audit agents (one per engine) ran the same
10-item SaaS readiness checklist against Importer, Translator, Detector,
and Policy. This doc folds their reports into one prioritised work list
for Phases 1–4. Each item has an owner phase, a one-line spec, and the
engines it touches.

**Two categories of work.** This doc covers two different things:

- **Audit findings** (the heatmap + CC-1..CC-4 + per-engine WARN clusters).
  Internal hygiene — does each engine do what it already claims to do,
  safely. Below.
- **Capability gaps** (the CG-* section near the end). Things the engines
  do NOT yet do, measured against the enterprise pattern that
  Firefly.ai / ControlMonkey / Brainboard converged on:

      1. Connect cloud account
      2. Scan & inventory (managed vs unmanaged)
      3. Codify
      4. Drift detection (incl. NEW unmanaged resources, not just changed ones)
      5. Policy gate
      6. Remediation

  Hygiene fixes ship first; capability work sits on top of clean
  engines. Both categories are demo-blocking.

**The 10-item checklist** (for reference):

1. No `os.chdir` in request paths
2. All filesystem paths plumbed as parameters (no module-relative
   `os.path.join` against unknown roots)
3. Engines return structured results (no `None` from public entry points)
4. No module-level mutable state shared across requests
5. Cold-start work is bounded and recoverable
6. Inputs validated before any external call
7. Subprocess calls have explicit timeouts
8. Concurrent requests for *different* projects/tenants do not collide
9. Tenant + project IDs flow through the call stack to logs
10. Structured logging (JSON-lines, request-context-tagged)

---

## Heatmap (PASS / WARN / FAIL per engine × item)

| # | Item | Importer | Translator | Detector | Policy |
|---|---|---|---|---|---|
| 1 | No chdir in request paths | PASS | PASS | PASS (CLI-only) | WARN (CLI-only) |
| 2 | Paths as parameters | PASS | PASS | PASS | PASS |
| 3 | Structured returns | **FAIL** (`run_workflow` → `None`) | PASS | PASS | PASS |
| 4 | No module-level mutable state | WARN (cumulative-ignores dict) | PASS | PASS | PASS |
| 5 | Bounded cold start | PASS | **FAIL** (Vertex AI init at import) | PASS | PASS |
| 6 | Inputs validated | PASS | PASS | PASS | PASS |
| 7 | Subprocess timeouts | **FAIL** (none on `gcloud`/`terraform`) | WARN (`LLM_TIMEOUT_SECONDS` defined, not wired) | **FAIL** (no `terraform plan` timeout) | PASS (conftest `timeout=30`) |
| 8 | Concurrency safety | PASS | WARN (intermediate YAML name collisions) | WARN (depends on gcp_client/policy) | PASS |
| 9 | Tenant/project context in logs | PASS (in returns) | PASS (in returns) | PASS (in returns) | PASS (in returns) |
| 10 | Structured logging | **FAIL** (bare `print()`) | **FAIL** (bare `print()`) | **FAIL** (bare `print()`) | **FAIL** (bare `print()`) |

Six **FAIL** cells. Five are cross-cutting; one is engine-specific.

---

## Cross-cutting work items (fix once, applies to all 4 engines)

### CC-1. Structured logging — FAIL × 4 engines (item #10)

Every engine uses bare `print()`. On Cloud Run this means logs land in
Cloud Logging as unstructured text, no per-request filtering possible.
A single failed client request is unfindable in the log stream.

**Fix.** A shared `common/logging.py` module exposing a
`get_logger(engine)` returning a stdlib logger configured for
JSON-lines output to stdout, with a `bind(tenant_id, project_id,
request_id)` helper that adds those fields to every record. Replace
`print(...)` with `logger.info(..., extra={...})` across the four
engines.

**Effort.** Half a day for the module + mechanical replacement. No
behaviour change, easy to land as one commit per engine for clean
diffs. **Phase 1** (lands first; Phases 2–4 can adopt as they touch
each engine).

### CC-2. Subprocess timeouts — FAIL × 2 engines (item #7)

Importer: `gcloud` calls have no `timeout=`. Detector: `terraform plan`
has no `timeout=`. A slow upstream hangs the request indefinitely;
Cloud Run's 60-minute request timeout is the only backstop, which is
60 minutes too long.

**Fix.** Wrap every `subprocess.run(...)` in our subprocess helpers
with explicit timeouts:

- `gcloud` calls — 60s (mostly `list` + `describe`, fast).
- `terraform init` — 600s (downloads providers; slow first run).
- `terraform plan` — 300s per target.
- `terraform import` — 120s per resource.

On `subprocess.TimeoutExpired`, raise a typed `UpstreamTimeout`
exception that the engine surfaces as `{"status": "timeout", "stage":
"plan", "elapsed_s": 300}` rather than letting it become a 500.

**Effort.** One day, importer + detector. **Phase 1** for importer,
**Phase 4** for detector.

### CC-3. Cold-start preflight — FAIL × 1, WARN × N (items #5, #6, #7, #8 in failure-modes matrix)

Each engine should fail fast at container boot if its prerequisites
are missing (gcloud, terraform, conftest binaries; ADC credentials;
Vertex AI access). Today the failures all surface on the first
request.

**Fix.** A `common/preflight.py` module with one function per check;
called once at Cloud Run boot from `app.py`. Output is a JSON report
the readiness probe reads. If any required check fails, the probe
returns 503 and Cloud Run does not route traffic to the broken
revision. Translator's Vertex AI module-level init becomes part of
the preflight (deliberate cold-start cost) instead of a hidden import
side-effect.

**Effort.** One day. **Phase 5** (Cloud Run packaging) — the checks
are easy, but the readiness-probe wiring is part of the deploy work.

### CC-4. Structured returns from `run_workflow` — FAIL × 1 (item #3, importer)

Importer's `run_workflow()` returns `None` today. UI code that wants
to render "imported 7 of 10 resources, skipped 3" cannot do it without
re-parsing logs.

**Fix.** Return a `WorkflowResult` dataclass:
```
@dataclass
class WorkflowResult:
    project_id: str
    imported: list[ResourceRef]
    skipped: list[SkippedResource]   # 403, unsupported type, etc.
    failed:   list[FailedResource]   # crashes, timeouts
    duration_s: float
```
Other three engines already return structured types; this aligns
importer with them.

**Effort.** Half a day. **Phase 1.**

### CC-5. Per-resource customer-facing outcome contract — surfaced by Phase 1 SMOKE

**Today.** `WorkflowResult` (C3, importer) carries only counts:
`{selected, imported, failed, skipped, duration_s}`. The CLI's
interactive HITL menu (`[1] snippet | [2] AI auto-correct | [3] skip`)
is operator-grade: customers should never see options that say
"provide an HCL snippet" — they don't write HCL, that's what they
pay us for. The CLI menu is fine for our internal use; the SaaS UI
needs a different surface area entirely.

The Phase 1 SMOKE made this concrete: 11 of 12 resources landed in
the `failed` bucket due to LLM-quality hallucinations on cluster
HCL + sibling-cascade. A customer seeing `failed=11` with no
per-resource explanation has no path forward. They need:

  - human-readable resource label ("VM 'poc-vm'", not
    `google_compute_instance.poc_vm`)
  - plain-English failure reason ("config has fields that don't
    map cleanly", not raw terraform "Missing required argument")
  - structured failure CATEGORY so the UI can render appropriate
    actions per category
  - actionable next-step buttons (not OMIT/IGNORE/SNIPPET commands)

**Spec.** Extend `WorkflowResult` with a per-resource list:

```python
@dataclass(frozen=True)
class ResourceOutcome:
    name: str                       # "poc-vm"
    type: str                       # "google_compute_instance"
    display_type: str               # "VM"  (customer-facing label)
    status: Literal["imported", "failed", "skipped"]
    failure_reason: Optional[str]   # enum below
    failure_message: str            # plain-English
    technical_details: str          # raw error (collapsible in UI)
    suggested_actions: list[str]    # ["auto_fix", "skip", "import_anyway"]

# Failure reason enum (initial set)
TRANSIENT_LLM_HALLUCINATION       # transient -> auto-correct, hidden from customer
OVERLAP_WITH_OTHER_RESOURCE        # boot disk vs VM, default-pool vs cluster
PROVIDER_UNSUPPORTED               # not in TF provider
MANAGED_BY_CLOUD_PROVIDER          # Autopilot pools etc.
PERMISSION_DENIED                  # gcloud 403
UPSTREAM_TIMEOUT                   # from C2 UpstreamTimeout
UNKNOWN                            # catch-all + technical_details
```

In `WorkflowResult`: add `resources: list[ResourceOutcome]` alongside
the existing counts. Counts remain for dashboard rollups; per-resource
list drives UI rendering.

Backend: tag failures in `_generate_and_save_hcl` and the per-resource
plan-verify loop with the appropriate `failure_reason`. Build the
`display_type` via a small `IMPORTER_TYPE_TO_DISPLAY` map.

UI: Codify tab renders the three buckets (imported / issues /
skipped) with the per-resource detail; expand-on-click for
`technical_details`; per-row action buttons keyed off
`suggested_actions`.

**Auto-correct as default for SaaS.** A separate but related
deliverable: in the SaaS path, run AI auto-correct AUTOMATICALLY
(silently, up to MAX_LLM_RETRIES) BEFORE surfacing failures.
Customer only sees a failure when the auto-correct loop genuinely
exhausts retries. Keeps the UI clean of transient
LLM-hallucination noise that the existing self-correction loop
fixes anyway.

**Effort.** 1 day backend + Phase 6 UI rendering. Backend lands
**Phase 5** (when we add the API layer Streamlit calls); UI lands
**Phase 6**.

### CC-6. Translator multi-file batch selection — surfaced by Phase 1 SMOKE

**Today.** Translator's CLI is single-file: prompts for a
`source_file_path` (`imported\dev-proj-470211\google_storage_bucket_*.tf`,
operator types the full path). For the SaaS this is wrong on three
axes:

  1. Customers shouldn't type paths.
  2. Customers don't know the on-disk filenames.
  3. Translating one file at a time defeats the bulk import they
     just did.

The Importer already has the right pattern (Stage 2 selection menu
with auto-discovered resources, comma-separated multi-select). The
Translator should mirror it.

**Spec.** Two layers:

  - **Backend (file discovery + batch processing):**
    `translator/run.py` adds a `discover_translatable_files(workdir)`
    helper that walks `imported/<project>/`, parses each `.tf` to
    extract `(resource_type, resource_name)`, returns a sorted list
    of `(file_path, resource_type, resource_name, display_label)`
    tuples. `run_translation_pipeline` becomes
    `run_translation_batch(target_cloud, source_paths)` returning a
    `TranslationResult` dataclass mirroring `WorkflowResult` from C3
    (per-file outcomes + counts + duration).

    Per-file failure isolation (Bug-B-style from C5.1): one file's
    LLM error must not kill the batch. Each file gets its own
    try/except wrapping the 3-stage pipeline.

  - **UI (Phase 6 Streamlit Translator tab):** Auto-discover
    files from the selected workdir, render a checkbox grid
    (`[ ] VM · poc-vm`, `[ ] Bucket · poc-smoke-bucket`, ...).
    Plus a target-cloud dropdown (AWS / Azure). Submit triggers
    the batch. Per-file progress streams via WebSocket / SSE.

**Effort.** 0.5 day backend + Phase 6 UI rendering. Backend lands
**Phase 3** (rolls into Translator hardening); UI lands **Phase 6**.

### CC-8. URN-as-displayName normalisation — surfaced + shipped in Phase 2 SMOKE 1 (CLOSED)

**Today (closed by P2-6 commit `70bf9c0`).** Cloud Asset Inventory
returns the full URN as `displayName` for several project-scoped
resource types whose canonical name IS the URN (KMS keyring + crypto
key, Pub/Sub topic + subscription, possibly other future types like
Secret Manager). Pre-P2-6 the importer used the URN verbatim as
`resource_name` (gcloud describe arg) AND `hcl_name_base` (HCL
resource label + filename). Three downstream failures:

  1. Resource line `resource "tf_type" "projects/.../keyRings/k"`
     is invalid HCL syntax (slashes not allowed in identifiers) ->
     `hcl_validation_failed reason=missing_resource_line`. Hit on
     keyring + topic + subscription in SMOKE 1.
  2. Filename `tf_type_projects/.../keyRings/k.tf` fails file write
     on Windows (slashes interpreted as directory separators). Hit
     on crypto_key in SMOKE 1.
  3. gcloud describe call uses the URN where a short name would do
     -- redundant, ugly, but functionally correct.

**Fix (shipped P2-6).** New `gcp_client.friendly_name_from_display(raw)`
helper: when raw contains "/", returns only the last path segment;
otherwise returns unchanged. Pure function, fail-safe on None /
empty. Used in `run.py _map_asset_to_terraform` for the non-SA
branch. Verified end-to-end in SMOKE 2: all 4 URN-style assets
imported successfully.

**Status.** CLOSED. Listed here for punchlist completeness +
provenance for future readers wondering why
`friendly_name_from_display` exists.

### CC-9. Few-shot golden examples for top 10 resource types

**Today.** LLM HCL generation relies on:
  * Pre-LLM: snapshot scrubbing (snapshot_scrubber, resource_mode,
    P2-7 nested empty blocks)
  * Schema oracle: per-attribute writability/required/computed flags
    fed into the prompt
  * Post-LLM: deterministic correction (post_llm_overrides for renames /
    deletions, post_llm_validation for empty-block scrubbing)
  * Self-correction loop: LLM regenerates with terraform error context
    on failures, up to MAX_LLM_RETRIES

This catches a LOT, but the LLM has no concrete reference shape for
the resource type it's generating -- it works from the schema +
input JSON alone. Smoke evidence (Phase 2 SMOKE 2) shows the LLM
still hallucinates field names (cluster_ipv4_cidr conflict),
nesting (cgroup_mode in wrong block), and v1-vs-v2 schema
confusion (Cloud Run container_concurrency).

The world-class enterprise approach to this exact problem is
**few-shot prompting with golden examples**: include 1-2
known-good HCL examples for the resource type in the system
prompt. The LLM pattern-matches against the golden output instead
of working from schema-spec alone. Per published industry results
(GitHub Copilot, Cursor, Anthropic's own research), this typically
lifts first-attempt accuracy from ~70% to ~90%+ on the covered types.

**Spec.** New `importer/golden_examples/<tf_type>.tf` directory
with one hand-written, plan-clean HCL file per type. Top 10 types
to cover (by smoke evidence + customer relevance):
  1. google_container_cluster (Standard mode)
  2. google_container_cluster (Autopilot mode)
  3. google_container_node_pool
  4. google_compute_instance
  5. google_storage_bucket
  6. google_kms_crypto_key
  7. google_cloud_run_v2_service
  8. google_pubsub_subscription
  9. google_compute_subnetwork
  10. google_service_account

New helper in `hcl_generator.py` that loads the example for the
target tf_type (if present) and prepends it to the system prompt
in a clearly-marked "REFERENCE EXAMPLE" section. Per-mode
specialisation (Autopilot vs Standard cluster) handled via
`<tf_type>__<mode>.tf` filename convention.

**Effort.** ~0.5 day per golden example × 10 types = ~5 days.
Half spent writing the example (verify it plan-clean against a
canonical instance), half on the prompt-builder + tests.

**Phase.** Phase 4 (per-engine quality wave alongside Detector +
Policy hygiene). Earlier doesn't make sense -- we need 2-3 more
SMOKE iterations to know which types actually have the worst
hallucination rates and prioritize accordingly.

**Why this matters for the demo.** Cluster + node_pool LLM
hallucinations are the dominant remaining failure mode after
P2-1..P2-11. Few-shot examples are the established way to close
that gap without architectural changes (constrained generation /
function calling) that would take weeks. Direct ROI: every
additional resource type that imports clean first-try is one less
"⚠ needs attention" card the customer sees in the SaaS UI.

#### Concrete failure-pattern inputs from Phase 2 SMOKEs

Three SMOKE iterations on dev-proj-470211 produced a corpus of
LLM hallucinations that golden examples must explicitly cover.
Each item below is a documented bug case CC-9 absorbs as a "do
NOT do this" annotation in the relevant golden example. Future
maintainer writing golden examples should grep for these
identifiers and confirm each is unrepresentable in the example
output.

**P2-12 — Cloud Run v2 `startup_cpu_boost` (v1 vestige)**
Surfaced: SMOKE 3, poc-cloudrun. Symptom:
`startup_cpu_boost = true` Unsupported argument. v2 schema
relocated this concept (it's now a sub-field of
`template.containers.startup_probe`, NOT a top-level template
field). Same class as P2-8's `container_concurrency` /
`latest_revision` (also v1-vestiges on v2). Golden
`google_cloud_run_v2_service.tf` must explicitly NOT contain
`startup_cpu_boost` at any nesting level the LLM might confuse.
Could fix per-symptom via post_llm_overrides
deletions (1 hour) but the cleaner fix is to give the LLM a
known-good v2 example.

**P2-13 — GKE Autopilot `ray_operator_config` (Autopilot-managed)**
Surfaced: SMOKE 3, poc-cluster (Autopilot). Symptom:
`ray_operator_config` Unsupported block type. Ray Operator is an
addon Autopilot manages internally; the provider rejects manual
config. Same pattern as P2-9.1's
`advanced_datapath_observability_config`. Could fix per-symptom
by adding `addons_config.ray_operator_config` to gke_autopilot's
`prune_paths` (literally one line) -- but P2-13 surfaced AFTER
P2-9.1 shipped, so we'd be re-opening the prune list every
SMOKE. Golden Autopilot cluster example with explicit "addons
not supported on Autopilot" comments is the structural fix.

**P2-14 — `insecure_kubelet_readonly_port_enabled` boolean-vs-enum**
Surfaced: SMOKE 3, poc-cluster-std + default-pool. Symptom:
`expected ... to be one of ["FALSE" "TRUE"], got false`.
Terraform schema represents this as a quoted-string enum (legal
values literally "TRUE" / "FALSE"), not a bare boolean. The LLM
emitted `insecure_kubelet_readonly_port_enabled = false`
(boolean), provider rejects. Different class from P2-12 / P2-13:
this is a value-type coercion, not a field-name or block-presence
issue. Could add a post_llm_overrides "type-coerce" mechanism
(new functionality, ~half day) -- but again, the LLM picks new
fields to mistype every smoke. Golden example showing the
correct quoted-enum syntax is the structural fix.

These three (plus dozens of similar long-tail cases not yet
surfaced) are the per-resource-type "negative example" content
each golden example needs to embody. The example doesn't
explicitly say "don't do X"; instead it shows the right way so
clearly that the LLM pattern-matches against it instead of
guessing.

### CC-7. LangChain dependency migration before LangChain 4.0 ships

**Today.** `llm_provider.py:40` uses `langchain_google_vertexai.ChatVertexAI`,
which emits this deprecation warning on every translator
invocation:

```
LangChainDeprecationWarning: The class `ChatVertexAI` was deprecated
in LangChain 3.2.0 and will be removed in 4.0.0. ... use
`langchain-google-genai` instead.
```

When LangChain ships 4.0 (no announced date but typically every
6-12 months for a major), every LLM call breaks. Surfaced by the
Phase 1 SMOKE Translator run.

**P3-7 scope correction (2026-04-27).** Initial punchlist entry
treated this as a simple "switch one import, change one class
name" job. It isn't. Investigation during P3-7:

  * `langchain-google-vertexai` (what we use today): Vertex AI
    backend, ADC-based auth, supports cross-project Service
    Account impersonation. This is what our Phase 5 Cloud Run
    architecture relies on (host-project SA impersonates a
    customer-tenant SA to read their assets).
  * `langchain-google-genai` (what the deprecation warning
    recommends): Google AI Studio API backend, API-key auth, no
    SA impersonation. **Different service entirely.** The
    deprecation warning is a misleading default for users with
    a Google account who just want to call Gemini; it is the
    wrong answer for our enterprise architecture.

The actual migration path for Vertex AI users is unclear from
the warning text alone. Likely options (need confirmation
against the package maintainers' guidance, NOT just the
deprecation message):

  1. A non-deprecated class within `langchain-google-vertexai`
     itself (the package may have introduced a successor class
     that the warning forgot to point at).
  2. A different package entirely (`langchain-google-vertex` or
     similar that's emerged since the warning was written).
  3. Drop LangChain for the LLM call layer and call
     `vertexai.generative_models.GenerativeModel` directly --
     LangChain's value-add for our two-message prompts is
     marginal; we already wrap retry/backoff ourselves
     (`safe_invoke`, P3-5). This is the lowest-risk option:
     vertex SDK is the canonical Google-supported path and has
     no LangChain-version-coupling.

**Decision (P3-7).** Do NOT blindly flip the import to
`langchain-google-genai` -- that would silently move us off
Vertex AI and break Phase 5 SA impersonation. Instead, this
commit:

  * Documents the deprecation context + the correct
    architecture-aware migration paths in `llm_provider.py`
    so future maintainer doesn't fall into the same trap.
  * Defers the actual code flip to **Phase 5 packaging** when
    `requirements.txt` pinning + Cloud Run dependency-resolution
    happens together. Coupling the migration with Phase 5 keeps
    the dep-version bump in one diff and one verification cycle
    rather than two (now + later).
  * The deprecation warning is non-fatal -- LangChain 4.0 has no
    announced ship date, so we have runway. P2 SMOKE evidence:
    the warning prints on every invocation but does not
    interfere with output quality or stability.

**Effort.** Half a day for the **investigation + flip** when
Phase 5 lands; ~zero for this docs-only commit.

**Status.** **Phase 5** (deferred from Phase 3 -- scope was
miscoded as "trivial dep flip" but is actually "service-layer
migration with backend implications").

---

## Engine-specific work items

### Importer

- **WARN→FIX: `_CUMULATIVE_IGNORES_PER_FILE`** (item #4). Module-level
  dict cleared at `run_workflow` entry. Works today because we run one
  workflow per process; under Cloud Run a single container serves
  many requests and the dict survives across them. Move to a per-call
  context or pass through the call stack. **Phase 1.**
- **WARN: `_SINGLETON` schema oracle cache** (item #4). Process-wide
  cache of provider schema. Safe (immutable per provider version) but
  the singleton pattern is fragile; document it or convert to an
  explicit cache injected at the entry point. **Phase 1.**
- **WARN: `time.sleep(1)` in request paths.** Found in retry loops;
  audit reports it slows requests on hot paths. Replace with bounded
  exponential backoff and a max-elapsed cap. **Phase 1.**
- **WARN: debug `print` left in `gcp_client.py:32-33`.** Remove.
  **Phase 1** (folded into CC-1).

### Translator

- **FAIL: cold-start at module import** (item #5). `llm_provider.py:21-44`
  initialises Vertex AI SDK + creates `ChatVertexAI` clients during
  module import. Startup latency, and the first request's tail latency
  spike. Move to lazy init wrapped behind `preflight()`. **Phase 3.**
- **WARN: `LLM_TIMEOUT_SECONDS` is defined but not wired** to
  `ChatVertexAI`. Fix during Phase 3 retry/timeout work.
- **WARN: tenant params not in signature.** The translator entry point
  takes paths but no tenant context, so structured logs (CC-1) cannot
  tag tenant. Add `tenant_id` and `project_id` parameters; pass through
  to logs. **Phase 3.**
- **WARN: intermediate YAML files collide under concurrency** (item #8).
  `_intermediate_blueprint_*.yaml` is a fixed name pattern; concurrent
  translations overwrite each other. Use `tempfile.mkdtemp()` per
  invocation or a request-id suffix. **Phase 3.**

### Detector

- **WARN: broad `except Exception` at line 1083.** Swallows real bugs
  as drift; tighten to specific exception types or re-raise with
  context. **Phase 4.**
- **WARN: `_state_path()` falls back to `os.getcwd()`.** This is the
  exact pattern that caused the per-project workdir refactor. Replace
  the fallback with a hard error — if no path was passed, the caller is
  buggy, do not silently use cwd. **Phase 4.**
- **WARN: concurrent dependency safety unverified.** Depends on
  `gcp_client` (importer-owned) and `policy` (own engine). After CC-1
  + CC-4 land, re-run the audit on detector specifically. **Phase 4.**

### Policy

- Cleanest of the four. Only the cross-cutting items + a small WARN
  on per-resource violation cap (a malicious or buggy .tf with 10k
  resources could blow up policy output). Add a configurable cap
  (default 1000 violations per run, then truncate with a warning).
  **Phase 4.**

---

## Phase mapping (what gets fixed when)

| Item | Phase | Estimated effort |
|---|---|---|
| CC-1 structured logging | Phase 1 (lands first) | 0.5 day |
| CC-2 subprocess timeouts (importer half) | Phase 1 | 0.5 day |
| CC-4 structured WorkflowResult | Phase 1 | 0.5 day |
| Importer engine-specific WARN cluster | Phase 1 | 0.5 day |
| Translator cold-start + tenant params + collisions | Phase 3 | 1 day |
| **CC-6 Translator multi-file batch selection** (backend) | **Phase 3** | **0.5 day** |
| **CC-7 LangChain dep migration** | **Phase 5** (deferred from P3, see scope-correction note) | **0.5 day** |
| CC-2 subprocess timeouts (detector half) | Phase 4 | 0.5 day |
| Detector engine-specific WARN cluster | Phase 4 | 0.5 day |
| Policy violation cap | Phase 4 | 0.25 day |
| **CG-1 unmanaged-resource tracking (Drift engine)** | **Phase 4** | **1.5 days** |
| **CG-2 Detector + Policy coverage parity** | **Phase 4** | **4 days** (revised 2026-04-27 with per-type table; ~25 new rules vs original 8-10 estimate) |
| **CG-3 Public-benchmark + Google-archive control mapping** | **Phase 4** | **2.5 days** (1 day overlaps CG-2's new-rule budget; CG-3 metadata baked into CG-2 rules at creation time) |
| **CG-4 IaC Status taxonomy parity (5-value enum)** | **Phase 5** | **1 day** (folds in alongside CC-5 backend) |
| **CG-5 Flags column parity (Policy + Git + Relationships)** | **Phase 5/6 split** | **2 days** (0.5d backend P5 + 1.5d UI P6) |
| **CG-6 Inventory tab as primary UI surface** | **Phase 6** | folded into existing Phase 6 UI budget |
| **CG-7 Failure isolation via quarantine pattern** | **Phase 4 hotfix (SHIPPED)** | **0.5 day** -- shipped same wave as P4-11/P4-12 SMOKE-4 hotfixes |
| **CC-9 Few-shot golden examples (top 10 types)** | **Phase 4** | **5 days** |
| CC-3 cold-start preflight | Phase 5 | 1 day |
| **CC-5 ResourceOutcome backend** | **Phase 5** | **1 day** |
| **CC-5 + CC-6 UI rendering** | **Phase 6** | **(folded into Phase 6 UI work)** |
| **CC-8 URN-as-displayName normalisation** | **CLOSED** (Phase 2 P2-6 / `70bf9c0`) | shipped |

**Total additional effort folded in:** ~22 days (5 days original
hygiene + 1.5 days CG-1 + 6 days surfaced by Phase 1 SMOKE: CC-5
backend, CC-6 backend, CC-7 dep migration, CG-2 coverage parity
[bumped to 4 days post-P4-PRE per-type enumeration] + 5 days
surfaced by Phase 2 SMOKEs: CC-9 few-shot examples + 1.5 days net
new from Phase 3-end strategic review: CG-3 control mapping minus
the 1-day CG-2 overlap + 3 days net new from Phase 4 Firefly-
inventory-research review: CG-4 taxonomy parity 1d + CG-5 flags
parity 2d, with CG-6 inventory-tab folded into existing Phase 6
UI budget). No standalone "fix the audit findings" phase; every
item folds into a phase that was already going to touch the
relevant engine.

**Items surfaced by Phase 1 SMOKE (2026-04-26):** CC-5, CC-6, CC-7,
CG-2. The smoke against `dev-proj-470211` exercised all 4 engines
end-to-end and made several latent design issues concrete:
customer-facing failure rendering needs structure (CC-5), Translator's
single-file CLI doesn't translate to a SaaS UX (CC-6), LangChain
deprecation will break us when 4.0 ships (CC-7), Detector + Policy
cover only 2/11 resource types vs the importer (CG-2).

**Items surfaced by Phase 2 SMOKEs (2026-04-27):**
  * **CC-8 URN-as-displayName** -- found + fixed in same iteration
    (P2-6 commit `70bf9c0`). KMS / Pub/Sub asset types return URN
    rather than short name as displayName; broke HCL labels +
    filenames. Now CLOSED.
  * **CC-9 Few-shot golden examples** -- the strategic enterprise
    answer to LLM HCL-quality variance, surfaced as the dominant
    remaining failure mode after P2-1..P2-11. Cluster cascade +
    nesting confusion = ~3 of 16 resources fail per smoke. Few-shot
    examples are the industry-standard fix that closes 70% -> 90%+
    first-attempt accuracy without architectural changes. Phase 4.

**Items surfaced by end-of-Phase-3 strategic review (2026-04-27):**
  * **CG-3 Public-benchmark + Google-archive control mapping** --
    triggered by the obvious "doesn't Google publish official
    policies?" question. Investigation found the GCP policy-library
    repo was archived 2025-08-20 with a different input format
    (CAI protobuf vs our Terraform plan JSON), so wholesale
    adoption is wrong. But the embedded `rego: |` blocks in their
    YAML templates carry Google's last-published numeric defaults
    (e.g. CMEK rotation = 1 year), which we mirror as
    "Google-archive default" provenance alongside CIS/NIST control
    IDs in our own rules. Three-source citation per rule. Phase 4.

**Items surfaced by Phase 4 Firefly-inventory research (2026-04-27):**
  * **CG-4 IaC Status taxonomy parity** -- Firefly + ControlMonkey +
    Brainboard all converged on a 5-value enum (codified /
    unmanaged / drifted / ghost / ignored). Phase 4 P4-3 shipped 3
    of 5 (`compliant`, `unmanaged`, `drifted`); CG-4 closes the
    gap by adding `ghost` (in state, missing from cloud) +
    `ignored` (per-tenant ignore-rule store). Phase 5 alongside
    CC-5 backend.
  * **CG-5 Flags column parity** -- Firefly's Inventory page surfaces
    6 flag icons per row (Policy / Mutations / Comments / Git /
    GitOps / Relationships). The 3 we can ship from data already
    in scope are Policy (already wired via `policy_tag`), Git
    (derivable from importer's HCL output + commit), Relationships
    (derivable from tfstate `dependencies`). The other 3 need
    persistent stores; deferred. Phase 5 backend + Phase 6 UI.
  * **CG-6 Inventory tab as primary UI surface** -- canonical layout
    with the column set above. Mirrors Firefly's UX so vendor
    evaluators recognize the surface within 30 seconds. Folds
    into Phase 6 UI work; no incremental days.

  Sources for the taxonomy + flag definitions:
  <https://docs.firefly.ai/introduction/terminology>,
  <https://docs.firefly.ai/detailed-guides/cloud-asset-inventory>.

The Phase 2 smokes also exposed the per-resource UX vocabulary
that CC-5 needs to render: 9 distinct `failure_reason` enum values
mapped to customer-facing actions (auto_fix, skip, import_anyway,
why_link, retry, show_what_to_grant, open_quota_request,
tell_us_about_use_case, show_details). See CC-5 spec for the full
table.

---

## Capability gaps vs the enterprise pattern

The audit (sections above) covers internal hygiene: do the engines do
what they already claim to do, safely. This section covers what the
engines do NOT yet do, measured against the enterprise spine that
Firefly.ai / Cloud Resilience and ControlMonkey converged on.

### CG-1. Unmanaged-resource tracking in the Drift engine

**Today.** Detector runs `terraform plan` per target. This catches
drift on resources that are *already in state* (managed resources
whose cloud values differ from their .tf), but is **completely blind**
to new resources clicked into the GCP console after baseline. A
client adopts 16 resources Monday, an admin spins up a new bucket
Tuesday in the console, our drift report Wednesday says "0 drift" —
the bucket is invisible until someone manually re-runs the importer's
inventory.

This is exactly the gap the "ghost asset" view in Firefly and
ControlMonkey closes, and is arguably the single most-demanded
feature in this product category. Without it, our Drift engine is a
strict subset of `terraform plan` — i.e. doesn't justify being a
separate engine.

**Spec.** Detector grows a *rescan* mode that:

  1. Re-runs the importer's enumeration over each supported resource
     type for the target project (read-only — same gcloud code path
     as the initial inventory).
  2. Loads the current `terraform.tfstate`'s resource list.
  3. Diffs the two sets. Returns a structured `DriftReport` with three
     buckets:

         drifted   : in state, cloud values differ from .tf  (current behaviour)
         compliant : in state, cloud matches .tf              (current behaviour)
         unmanaged : in cloud, NOT in state                   (NEW)

  4. UI surfaces `unmanaged` in its own tab/section with a one-click
     **"Codify this"** action that hands the resource off to the
     Importer's writer. Closes the loop: discover -> codify -> baseline,
     in one flow.

**Why this is Phase 4, not earlier.** Detector hygiene fixes (CC-2
timeouts, `_state_path` fallback, broad-except tighten) need to land
first; capability work should sit on top of a clean engine, not under
one. The enumeration code is already written (it's the importer's
inventory step); the new code is the diff logic + the structured
return type + threading through the UI.

**Estimate.** 1.5 days:

  - 0.5 day — extract the importer's enumeration into a reusable
    `inventory(project_id) -> set[ResourceRef]` function (it already
    exists implicitly; needs a clean entry point).
  - 0.5 day — the diff logic + `DriftReport` dataclass + tests.
  - 0.5 day — UI tab in Phase 6 (the Phase 6 line item already
    includes a Drift tab; this just expands its scope).

**Why this matters for the demo.** A vendor evaluating us against
Firefly will ask "show me unmanaged resources" within the first 5
minutes. Without CG-1, the answer is "run a fresh import" — which
is the manual workflow they're paying us to replace.

### CG-2. Detector + Policy resource-coverage parity with Importer — surfaced by Phase 1 SMOKE

**Today.** Detector and Policy share a single `IN_SCOPE_TF_TYPES`
filter (defined in `detector/config.py`) that currently lists only
two types:

  - `google_compute_instance`
  - `google_storage_bucket`

The Phase 1 SMOKE imported 11 resource types into state but
Detector/Policy could only drift-check / compliance-scan 2 of them
(`2 in scope, 9 out of scope` was printed verbatim). The other 9
(disks, firewall, network, subnetwork, clusters, node pool, SA)
were silently skipped — no drift report, no policy evaluation, no
indication to the customer that they're flying blind on those
types.

This is the most embarrassing gap a vendor demo would surface: a
customer scans their project, the importer finds 30 resources, and
the drift / policy report only covers 2 of them. The rest are
invisible.

**Spec.** Two parts:

  1. Expand `IN_SCOPE_TF_TYPES` to match `ASSET_TO_TERRAFORM_MAP`
     (importer/config.py). Every type the importer can ingest
     should also be drift-check-able and policy-scan-able.

  2. For each newly-in-scope type, add at minimum:
     - **Detector**: a `cloud_snapshot` fetcher (most are already
       reachable via the importer's existing `gcp_client.describe`
       wrappers — needs a thin reuse layer).
     - **Policy**: at least one Rego rule per type — start with
       `mandatory_labels` (the common policy already runs on every
       type) plus 1-2 type-specific rules where the security
       community has a strong recommendation
       (e.g. `gke_cluster_private_nodes`, `compute_disk_encryption`,
       `firewall_no_open_ssh`). For each rule, follow CG-3's
       three-source methodology (Google-archive + CIS + NIST) --
       the table below pre-identifies which GCP-archive templates
       to mine for each importer type.

  3. For types where drift detection requires extra logic
     (composite resources like `google_container_cluster` with
     nested node-pool configs), tag those as "drift-aware" vs
     "drift-stub" so the UI can show "we monitor this type, but
     the drift checker is conservative — false negatives possible".

**Concrete coverage gap (added 2026-04-27).** Importer supports 17
types; only 2 (`google_compute_instance`, `google_storage_bucket`)
have Rego rules today. Per-type extension table below; `Mine from`
column lists matching templates in the archived
GoogleCloudPlatform/policy-library so CG-3's methodology applies
verbatim.

| Importer tf_type | Today | Mine from (GCP-archive templates) | Suggested rule(s) |
|---|---|---|---|
| google_compute_instance | 3 rules | (already mined P4-PRE) | (extend with `gcp_compute_block_ssh_keys_v1`, `gcp_compute_enable_oslogin_project_v1`, `gcp_compute_ip_forward`) |
| google_compute_disk | — | `gcp_compute_disk_resource_policies_v1` + `gcp_cmek_settings_v1` (proxy) | `disk_cmek_required` (CIS GCP 4.7), `disk_snapshot_policy_attached` |
| google_compute_firewall | — | `gcp_restricted_firewall_rules_v1`, `gcp_network_enable_firewall_logs_v1` | `firewall_no_open_ssh` (CIS GCP 3.6), `firewall_no_open_rdp` (CIS GCP 3.7), `firewall_logs_enabled` |
| google_compute_address | — | (none directly; relates to `gcp_compute_external_ip_address`) | `address_purpose_documented` (industry consensus) |
| google_compute_network | — | `gcp_network_routing_v1`, `gcp_network_restrict_default_v1` | `network_no_default_vpc` (CIS GCP 3.1), `network_routing_mode_regional` |
| google_compute_subnetwork | — | `gcp_network_enable_flow_logs_v1`, `gcp_network_enable_private_google_access_v1` | `subnet_flow_logs_enabled` (CIS GCP 3.8), `subnet_private_google_access` |
| google_compute_instance_template | — | (inherits `gcp_compute_external_ip_address` semantics) | inherit applicable instance rules via shared package |
| google_container_cluster | — | 14 archived templates: `gke_enable_workload_identity_v1`, `gke_enable_shielded_nodes_v1`, `gke_enable_binauthz_v1`, `gke_private_cluster_v1`, `gke_master_authorized_networks_enabled_v1`, `gke_legacy_abac_v1`, `gke_disable_legacy_endpoints_v1`, `gke_disable_default_service_account_v1`, `gke_cluster_version_v1`, `gke_enable_alias_ip_ranges`, `gke_enable_stackdriver_logging_v1`, `gke_enable_stackdriver_kubernetes_engine_monitoring_v1`, `gke_restrict_pod_traffic_v2`, `gke_restrict_client_auth_methods_v1` | richest coverage in the archived library; pick top 4 for Phase 4: `cluster_workload_identity` (CIS GCP 7.x), `cluster_private_endpoint`, `cluster_legacy_abac_disabled`, `cluster_master_authorized_networks` |
| google_container_node_pool | — | `gke_node_auto_repair_v1`, `gke_node_auto_upgrade_v1`, `gke_allowed_node_sa_v1`, `gke_container_optimized_os` | `node_pool_auto_upgrade` (CIS GCP 7.x), `node_pool_auto_repair`, `node_pool_uses_cos` |
| google_service_account | — | `gcp_iam_restrict_service_account_creation_v1`, `gcp_iam_restrict_service_account_key_age_v1` (default 90d), `gcp_iam_restrict_service_account_key_type_v1` | `sa_key_age_max_90_days` (CIS GCP 1.7), `sa_no_user_managed_keys` (CIS GCP 1.4) |
| google_storage_bucket | 4 rules | (already mined P4-PRE) | (extend with `gcp_storage_logging_v1`, `gcp_storage_location_v1`) |
| google_sql_database_instance | — | 7 archived templates: `gcp_sql_backup_v1`, `gcp_sql_maintenance_window_v1`, `gcp_sql_public_ip_v1`, `gcp_sql_ssl_v1`, `gcp_sql_world_readable_v1`, `gcp_sql_allowed_authorized_networks_v1`, `gcp_sql_instance_type_v1` | pick top 3: `sql_no_public_ip` (CIS GCP 6.5), `sql_ssl_required` (CIS GCP 6.4), `sql_backup_enabled` (CIS GCP 6.7) |
| google_kms_key_ring | — | (covered via crypto_key) | inherit |
| google_kms_crypto_key | — | `gcp_cmek_rotation_v1` (default 1y; CIS = 90d), `gcp_cmek_settings_v1` (protection_level / algorithm / purpose) | `key_rotation_max_90_days` (CIS GCP 1.10), `key_protection_level_hsm_for_critical`, `key_algorithm_allowlist` |
| google_cloud_run_v2_service | — | **NONE in archived library** — Cloud Run wasn't covered | source from industry consensus + CIS Controls v8: `cloudrun_no_public_invoker`, `cloudrun_min_instances_documented` |
| google_pubsub_topic | — | **NONE in archived library** | `pubsub_topic_cmek_required` (CIS Controls v8 3.11), `pubsub_topic_iam_no_allusers` |
| google_pubsub_subscription | — | **NONE in archived library** | `pubsub_sub_dead_letter_configured`, `pubsub_sub_iam_no_allusers` |

**Coverage scoring of the gap:**
  * 15 of 17 importer types have NO Rego rule today.
  * 12 of 15 uncovered types have at least one GCP-archive template
    to mine from (apply CG-3 methodology).
  * 3 of 15 uncovered types (`google_cloud_run_v2_service`,
    `google_pubsub_topic`, `google_pubsub_subscription`) have NO
    archived template — Google never wrote policies for these
    services. Must source from CIS Controls v8 + industry
    consensus alone for these.

Total new rules to write: ~25 (the table's "Suggested rule(s)"
column tallies to roughly 23-28 depending on how compositely some
are bundled).

**Why this is Phase 4, not earlier.** Detector hygiene fixes (CC-2
detector half, broad-except tighten, _state_path fallback) and CG-1
unmanaged tracking should land first. CG-2 sits on top of a clean
detector — adding more scope to a buggy engine multiplies the bug
surface.

**Estimate (revised 2026-04-27 with concrete table).** ~4 days:

  - 0.5 day — extend IN_SCOPE_TF_TYPES + audit each type's
    cloud_snapshot reachability
  - 0.5 day — fill gaps with thin reuse-from-importer wrappers
  - 3 days — write ~25 Rego rules per the table above, applying
    CG-3's three-source provenance methodology to each. Mining
    each archived template adds ~5-10 minutes per rule (much less
    than writing from scratch); the 3-day budget reflects that
    leverage.

(Previously estimated 2 days for "8-10 new rules"; the
post-P4-PRE table shows the real surface is ~25 rules. Doubling
the budget to match.)

**Why this matters for the demo.** Coupled with CG-1 (unmanaged
tracking), CG-2 is what makes the Drift + Policy engines actually
useful. Today they're a tech demo on 2 resource types. After
CG-2 they're production-grade across the importer's whole footprint.

### CG-3. Public-benchmark + Google-archived control mapping in policy rules — surfaced 2026-04-27

**Today.** Our `.rego` rules (11 files: 3 GCE + 4 GCS + 3 EC2 + 4 S3 +
2 common) enforce sensible defaults but carry no provenance metadata.
A customer reading a violation message sees `bucket lacks
encryption_default` with no answer to "says who?". A vendor demo
prospect comparing us to Firefly / ControlMonkey / Prisma will
note the absence of CIS / NIST / SOC2 control IDs immediately —
those vendors all surface the control catalog mapping as a
top-level UI element.

**Why we don't just adopt GoogleCloudPlatform/policy-library.**
Investigated 2026-04-27 in response to the obvious "doesn't Google
publish policies?" question. Findings:

  1. **Upstream archived 2025-08-20.** Read-only repo, no new
     policies, no security fixes, no support for new GCP services.
     Adopting it = adopting a dead dependency.
  2. **Wrong input format for our use case.** Their policies are
     Gatekeeper-style ConstraintTemplates (YAML wrapping Rego)
     consuming Cloud Asset Inventory protobuf
     (`input.asset.resource.data.*`). We use plain `.rego` via
     conftest consuming Terraform plan JSON
     (`input.resource_changes[_].change.after.*`). Different
     shapes, different evaluation timing (their policies validate
     resources ALREADY in cloud; ours gate the proposed change
     BEFORE apply).
  3. **No CIS/NIST citations in their templates either.** Random
     sample of 4 templates (storage_world_readable, cmek_rotation,
     iam_sa_key_age, storage_retention) — none cite a public
     control catalog. Their library was a parameterised template
     engine, not a compliance-mapped library.

**But there IS extractable value.** Three signals from the
embedded `rego: |` block in each YAML template (Apache 2.0
licensed; safe to adapt):

  1. **Numeric defaults Google last published.** E.g. CMEK rotation
     default `31536000s` (1 year). Citing Google's last-recommended
     value alongside our chosen value is a "we did our homework"
     signal even when we choose stricter.
  2. **Configurable-vs-hardcoded design choices.** Storage retention
     days = no default, operator must specify. Rotation period =
     default with override. Reflects Google's view on which knobs
     are org-policy vs universal.
  3. **Sentinel patterns.** E.g. `99999999s` as "never rotates"
     fallback — missing field always triggers fail. Worth mirroring.

Often Google's defaults are LESS strict than CIS. CMEK rotation:
Google = 1 year, CIS GCP 1.10 = 90 days. Right call is to default
to the stricter (CIS) value AND cite the looser (Google-archive)
value for transparency.

**Spec.** Three-line provenance comment block at the top of every
Rego rule:

```rego
# Source: GoogleCloudPlatform/policy-library (archived 2025-08-20)
#         <ConstraintTemplateName> -- last published default <X>
# Standard: CIS GCP <section.rule> -- recommends <Y>
# NIST: SP 800-53 <control-family.id>
# We default to: <chosen value> (rationale: <stricter|matches Google|...>)
package <existing.package.path>
```

Helper function `policy_metadata()` extracts these into the
`details` field of every deny[] rule so the UI renders:

  * Plain-English failure explanation
  * Three control IDs with hyperlinks (CIS / NIST / Google-archive)
  * "Why this default" rationale text

**Spec details.** Five-step rollout in Phase 4:

  1. (0.25 day) Define the metadata header convention + write a
     conftest test that asserts every `.rego` in `policy/policies/`
     carries a complete metadata block.
  2. (0.5 day) Audit each of the existing 11 rules: cross-check
     against archived GoogleCloudPlatform/policy-library template
     for matching tf_type. Record the Google-default value, the
     CIS recommendation, and our chosen value in a markdown table
     (`docs/policy_provenance.md`).
  3. (0.5 day) Add the metadata block to each existing rule.
     Mechanical edit; deterministic.
  4. (1 day) For each NEW rule added under CG-2 (8-10 rules
     across the newly-in-scope types), follow the same
     three-source calibration: Google-archive default + CIS
     recommendation + chosen value. Cite all three.
  5. (0.25 day) Wire the metadata into policy violation output
     so failures render with control IDs (UI work folds into
     Phase 6).

**CIS coverage targets** (the 5-10 NEW rules from CG-2):
  * CIS GCP 1.10 — KMS rotation period <= 90 days
    (`google_kms_crypto_key`)
  * CIS GCP 3.6 — VPC firewall does not allow 0.0.0.0/0 on SSH
    (`google_compute_firewall`)
  * CIS GCP 3.7 — VPC firewall does not allow 0.0.0.0/0 on RDP
    (`google_compute_firewall`)
  * CIS GCP 4.10 — VPC Flow Logs enabled on subnetworks
    (`google_compute_subnetwork`)
  * CIS GCP 5.1 — Cloud SQL not publicly accessible
    (`google_sql_database_instance`, future)
  * CIS GCP 7.x — GKE private nodes + workload identity + node
    auto-upgrade (`google_container_cluster` /
    `google_container_node_pool`) — covers 3 rules
  * CIS GCP 8.x — Logging sink to immutable bucket retention
    (`google_logging_project_sink`, future)

That's 5 immediately-applicable rules (KMS, FW SSH, FW RDP, Flow
Logs, GKE private nodes) + 3 more once we add the supporting
types. Total 8 NEW rules with full three-source provenance.

**Why this is Phase 4.** Cleanest sequence: CG-2 expands type
coverage and adds the rules, CG-3 layers metadata + benchmark
mapping on top in the same Phase. One PR per scope expansion;
metadata baked in from rule creation, not retrofitted later.

**Estimate.** 2.5 days:
  * 0.25 day metadata convention + conftest enforcement test
  * 0.5 day audit + provenance table for existing 11 rules
  * 0.5 day backfill metadata into existing rules
  * 1 day add metadata as part of CG-2's 8-10 new rules (folded
    in, not on top)
  * 0.25 day violation-output wiring (UI consumption deferred to
    Phase 6)

**Why this matters for the demo.** Compliance auditability is
table stakes for any enterprise prospect with a SOC2 or PCI
program. "We enforce CIS GCP 1.10" is parseable to a
non-engineer; "we check rotation period" is not. The Google-
archive citation is a credibility multiplier specifically with
GCP-focused buyers — they'll recognise the project name.

### CG-4. IaC Status taxonomy parity with Firefly / ControlMonkey — surfaced 2026-04-27

**Today.** Phase 4 P4-3 (`5c6fb06`) shipped `DriftReport` with three
buckets: `drifted`, `compliant`, `unmanaged`. This covers 3 of the
5-value enum that Firefly and the rest of the IaC governance
category use as their canonical inventory status:

| Firefly (canonical) | Our DriftReport today |
|---|---|
| Codified | `compliant` (in state) |
| Unmanaged | `unmanaged` (NEW in P4-3) |
| Drifted | `drifted` (shape ready; not populated until drift_check wired) |
| Ghost | -- not surfaced as distinct bucket; folds into the diff_engine `error` field |
| Ignored | -- no per-tenant ignore-rule store yet |

A vendor evaluating us alongside Firefly (the dominant player) will
expect to see the 5-value enum verbatim. Operators reading our UI
who've used Firefly should not have to learn a new vocabulary --
"Unmanaged" means the same thing in both products; ours just stops
short of the full taxonomy today.

**Source for the taxonomy:** Firefly Terminology Glossary
(<https://docs.firefly.ai/introduction/terminology>). Verbatim
definitions cross-checked against ControlMonkey + Brainboard
product docs -- all three vendors converged on these 5 values.

**Spec.** Two parts:

  1. **Extend `DriftReport`** (`detector/drift_report.py`) with
     two new fields:
     ```python
     ghost: List[ManagedResource] = field(default_factory=list)
       # in state, missing from cloud (deleted out-of-band)
     ignored: List[str] = field(default_factory=list)
       # tf_addresses skipped per IaC-Ignore rules
     ```
     Update `exit_code` to also flag non-zero on `ghost` (a deleted
     resource the operator didn't intend to delete is a finding).
     Update `as_fields()` to include the two new counts.

  2. **`Detector.rescan()` populates `ghost`** by calling
     `cloud_snapshot.fetch_snapshots(state_resources)` on the
     in-scope subset; any address with snapshot==None +
     `subprocess.CalledProcessError` from gcloud describe lands
     in `ghost`. This reuses the existing fail-soft snapshot
     fetcher; no new gcloud calls invented.

  3. **`ignored` rule store** -- minimum viable: a JSON file at
     per-project workdir `imported/<project>/ignore_rules.json`
     with shape `{"rules": [{"tf_address": "...", "reason": "..."}]}`.
     Loaded at rescan time; `ignored` populated from this list
     intersected with current state. Phase 6 UI gets the
     "mark as ignored" button; this commit ships the storage
     format + loader so the API is ready when the UI lands.

**Estimate.** 1 day:
  * 0.25 day extend DriftReport + tests (mirrors P4-3 pattern)
  * 0.25 day rescan() ghost detection wiring + tests
  * 0.5 day ignore_rules.json format + loader + dataclass tests

**Phase.** Phase 5 -- folds in alongside CC-5 ResourceOutcome
backend work (CC-5 should adopt the 5-value enum from CG-4 so the
two surfaces speak the same vocabulary).

**Why this matters for the demo.** A vendor demo that says "we
support these 3 buckets" while Firefly says "we support these 5"
gets dinged before the 5-min mark. Closing the gap is a few
hours' work and converts a perception delta into parity.

### CG-5. Flags column parity (Policy + Git + Relationships) — surfaced 2026-04-27

**Today.** Phase 1 wired `policy_tag` onto `ResourceDrift` so a
drifted resource with policy violations carries a tag suffix in
the report. This is half of Firefly's "Policy" flag concept.
Firefly's Inventory page surfaces 6 flags as visual icons on
each resource row:

  Policy / Mutations / Comments / Git / GitOps / Relationships

(See <https://docs.firefly.ai/detailed-guides/cloud-asset-inventory>.)

For our SaaS UI, the most impactful are the 3 we can ship from
data we already have or trivially derive:

  * **Policy** — already wired; just needs UI rendering.
  * **Git** — derivable from "which `.tf` file the importer wrote
    + which commit committed it". Needs source-of-truth tracking
    once Cloud Run packaging adds a Git checkout.
  * **Relationships** — derivable from Terraform state's
    `dependencies` array + HCL reference parsing
    (`google_container_node_pool.cluster = ...`). No new data
    sources needed.

The other 3 (Mutations / Comments / GitOps) need persistent
stores or external integrations and are deferred to a Phase 6+
follow-up.

**Spec.** Three layers:

  1. **Backend** (`detector` + new `inventory_flags.py`):
     A pure helper that computes the flags for a given resource
     from data already in scope:
     ```python
     def compute_flags(
         resource: ManagedResource | CloudResource,
         drift: Optional[ResourceDrift] = None,
         state: Optional[dict] = None,  # raw tfstate dict for deps
         hcl_path: Optional[str] = None,  # path inside imported/
     ) -> list[Literal["policy", "git", "relationships"]]:
     ```
     Returns the subset of flags that apply.

  2. **CC-5 ResourceOutcome adopts** the `flags` field (per the
     CC-5 spec note in CG-4). Single source of truth for both
     the inventory page and the per-resource detail drawer.

  3. **UI** (Phase 6): icon row in the inventory table; click any
     flag to filter the table to "only resources with this flag"
     (Firefly's "click flag in upper right to filter" UX).

**Phase.** Phase 6 -- needs the UI to be useful; the backend
half is small (~half a day) and folds into Phase 5's CC-5 work.

**Estimate.** 2 days total:
  * 0.5 day backend `compute_flags()` + tests (Phase 5)
  * 1.5 day Phase 6 UI rendering + filter chips

### CG-6. Inventory tab as the SaaS UI's primary surface — surfaced 2026-04-27

**Today.** No SaaS UI exists yet (Phase 6 work). When it lands,
the natural primary surface is a single Inventory page with the
columns the rest of the category converged on:

  Cloud / Type / Name / Env / IaC Status / Flags / Location / Owner

Per Firefly's `Exploring the Inventory` doc and our own column
inventory in the related research note, this layout makes the
product immediately recognizable to anyone who's used Firefly /
ControlMonkey / Brainboard.

**Spec.** Streamlit page (`app/inventory.py`) with:

  * **Table layout** (left to right):
      Cloud (icon) | Type (`tf_type`) | Name | Env (label) |
      **IaC Status** (color-coded badge: green=codified,
      blue=unmanaged, yellow=drifted, red=ghost, gray=ignored) |
      **Flags** (icon row: ⚠ Policy, 🔗 Git, ⛓ Relationships) |
      Location | Owner (deferred -- needs audit-log integration)

  * **Click any row** -> side panel with the full ResourceOutcome
    detail (ties into CC-5 + CC-6 rendering work). Mirrors
    Firefly's drawer pattern.

  * **Filter chips** above the table:
      [All] [Codified N] [Unmanaged N] [Drifted N] [Ghost N]
      [Ignored N]
    Plus per-flag chips:
      [Policy violations N] [Has Git N] [Has Relationships N]

  * **Action buttons** in the side panel context-aware:
      - Unmanaged -> "Codify this" (hands to importer)
      - Drifted -> "Restore" / "Accept" / "Show diff"
      - Ghost -> "Recreate" / "Drop from state"
      - Codified -> (no action; informational)

**Phase.** Phase 6 (folds into existing UI work). The CG-4 +
CG-5 backend pieces ship in Phase 5 so by the time Phase 6
lands the data plumbing is ready to render.

**Estimate.** Folded into the Phase 6 UI budget -- no
incremental days beyond the existing Phase 6 line item.

**Why this matters for the demo.** Vendor evaluators score on
"how recognizable is this UI" within the first 30 seconds. A
Streamlit page that mirrors Firefly's column set + flag
semantics scores immediately; a custom layout requires
explaining the vocabulary before any feature gets evaluated.

### CG-7. Failure isolation via quarantine pattern — surfaced + SHIPPED in SMOKE 4 hotfix wave (2026-04-27)

**Today (after CG-7 ship).** When a resource's per-resource plan
verification fails, the importer now offers TWO paths:

  * **CLI / interactive (default):** existing 3-option HITL menu
    unchanged. Operator picks snippet / AI self-correct / skip
    per resource. Right tool for our local debugging.

  * **Headless (SaaS Cloud Run):** when env var
    ``IMPORTER_AUTO_QUARANTINE=1`` is set, the importer skips the
    HITL menu and quarantines self-broken resources automatically.
    Each quarantined resource gets:
      1. ``.tf`` file moved to ``<workdir>/_quarantine/<filename>``
      2. ``terraform state rm <addr>`` to keep state consistent
         (revert-on-failure if state-rm fails, so workdir + state
         can never diverge)
      3. A ``<filename>.quarantine.txt`` sidecar explaining WHY
         (the truncated terraform error)
    After quarantine, plan verification re-runs on the survivors --
    previously "blocked-by-sibling" resources auto-promote to
    imported. Result: maximum salvage, zero customer-facing HITL.

**WorkflowResult adopts ``needs_attention`` field.** Counts the
quarantined resources separately from ``failed`` (which now means
"couldn't even import" -- a stronger negative signal). Accounting
invariant: ``imported + needs_attention + failed + skipped ==
selected``. ``exit_code`` is 1 when needs_attention > 0 (gates CI).

**Pre-fix problem statement:** ``terraform plan -target=ADDR``
parses every ``.tf`` file in the workdir BEFORE honouring
``-target``. One broken ``.tf`` cascade-blocks plan verification on
EVERY other resource. SMOKE 4 hit this concretely: 1 self-broken
resource (poc-cloudrun, P4-11 startup_cpu_boost) + 13
blocked-by-sibling resources, all reported as "failed" until the
operator fixed the broken one interactively.

**Spec.** Three new files + one extension:

  * ``importer/quarantine.py`` (NEW) -- pure function module:
      - ``is_auto_quarantine_enabled() -> bool`` (env var parse)
      - ``quarantine_path(workdir) -> str`` (lazy dir creation)
      - ``quarantine_resource(workdir, tf_address, hcl_filename,
        reason) -> bool`` (move + state_rm + sidecar; revert on
        partial failure)
  * ``importer/terraform_client.py`` (EXTENDED) -- public
    ``state_rm(tf_address, *, workdir) -> bool``. Mirrors the
    existing in-line state-rm in ``import_resource()``; same
    timeout budget (60s).
  * ``importer/results.py`` (EXTENDED) -- ``needs_attention: int = 0``
    field on ``WorkflowResult``. ``exit_code`` returns 1 when
    needs_attention > 0 (forces CI to gate on quarantined items).
  * ``importer/run.py`` (WIRED) -- in the failed_imports loop,
    when ``IMPORTER_AUTO_QUARANTINE`` is set, skip the HITL menu
    and run quarantine + replan instead.

**Tests.** 13 new (4 needs_attention + 9 quarantine):
  * ``WorkflowResultNeedsAttentionTests`` -- field carries count,
    defaults to 0, exit_code semantics
  * ``IsAutoQuarantineEnabledTests`` -- env var parsing (truthy /
    falsy / unset / whitespace)
  * ``QuarantinePathTests`` -- pure function; doesn't create dir
  * ``QuarantineResourceHappyPathTests`` -- moves file + invokes
    state_rm + writes sidecar
  * ``QuarantineResourceFailureModeTests`` -- missing source file,
    state_rm failure with file-revert (workdir+state stay
    consistent)

terraform_client.state_rm is mocked so tests don't shell out.

**Phase mapping update.** P4-11 / P4-12 / CG-7 form the SMOKE 4
hotfix wave -- all three landed before the user's SMOKE 4 re-run.

**Future work (Phase 5):** replace the env-var gate with an
explicit ``run_workflow(headless=True)`` kwarg once the call
surface gets a Phase 5 refresh. Same logic; cleaner contract.

---

## What this punchlist intentionally does NOT include

- **Performance optimisation.** The audit is correctness/safety, not
  speed. If the demo runs slowly, that is a Phase 6 concern (UI-level
  perceived latency: progress bars, streaming).
- **New features.** No row above adds capability. All rows pin or
  harden behaviour the engines are already trying to do.
- **Multi-cloud.** AWS support is Phase 3 work item, not in this audit.
- **Auth model.** Cross-project SA impersonation is Phase 5.

If something feels missing here, check the failure modes matrix
(`docs/saas_failure_modes.md`) — that doc covers client-observable
behaviour, this doc covers internal engineering hygiene.
