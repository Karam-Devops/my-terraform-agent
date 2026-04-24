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
| CC-2 subprocess timeouts (detector half) | Phase 4 | 0.5 day |
| Detector engine-specific WARN cluster | Phase 4 | 0.5 day |
| Policy violation cap | Phase 4 | 0.25 day |
| **CG-1 unmanaged-resource tracking (Drift engine)** | **Phase 4** | **1.5 days** |
| CC-3 cold-start preflight | Phase 5 | 1 day |

**Total additional effort folded in:** ~6.5 days (5 days hygiene
+ 1.5 days CG-1), distributed across phases that were already going
to touch each engine. No standalone "fix the audit findings" phase.

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
