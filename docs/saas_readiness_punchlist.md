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
| **CG-2 Detector + Policy coverage parity** | **Phase 4** | **2 days** |
| **CC-9 Few-shot golden examples (top 10 types)** | **Phase 4** | **5 days** |
| CC-3 cold-start preflight | Phase 5 | 1 day |
| **CC-5 ResourceOutcome backend** | **Phase 5** | **1 day** |
| **CC-5 + CC-6 UI rendering** | **Phase 6** | **(folded into Phase 6 UI work)** |
| **CC-8 URN-as-displayName normalisation** | **CLOSED** (Phase 2 P2-6 / `70bf9c0`) | shipped |

**Total additional effort folded in:** ~15.5 days (5 days original
hygiene + 1.5 days CG-1 + 4 days surfaced by Phase 1 SMOKE: CC-5
backend, CC-6 backend, CC-7 dep migration, CG-2 coverage parity +
5 days surfaced by Phase 2 SMOKEs: CC-9 few-shot examples). No
standalone "fix the audit findings" phase; every item folds into a
phase that was already going to touch the relevant engine.

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
       `firewall_no_open_ssh`).

  3. For types where drift detection requires extra logic
     (composite resources like `google_container_cluster` with
     nested node-pool configs), tag those as "drift-aware" vs
     "drift-stub" so the UI can show "we monitor this type, but
     the drift checker is conservative — false negatives possible".

**Why this is Phase 4, not earlier.** Detector hygiene fixes (CC-2
detector half, broad-except tighten, _state_path fallback) and CG-1
unmanaged tracking should land first. CG-2 sits on top of a clean
detector — adding more scope to a buggy engine multiplies the bug
surface.

**Estimate.** 2 days:

  - 0.5 day — extend IN_SCOPE_TF_TYPES + audit each type's
    cloud_snapshot reachability
  - 0.5 day — fill gaps with thin reuse-from-importer wrappers
  - 1 day — write 1-2 Rego rules per type (8-10 new rules), test

**Why this matters for the demo.** Coupled with CG-1 (unmanaged
tracking), CG-2 is what makes the Drift + Policy engines actually
useful. Today they're a tech demo on 2 resource types. After
CG-2 they're production-grade across the importer's whole footprint.

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
