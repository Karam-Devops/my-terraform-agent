# SMOKE 4 Prep + Run Guide (Phase 4 closeout)

**Purpose.** End-to-end exercise of all 4 engines after Phase 4
landed CG-1 (unmanaged tracking), CG-2 (28 new policy rules + scope
expansion to 17 types), CG-3 (provenance enforcement), and CC-9
(10 few-shot golden examples).

**Project.** `dev-proj-470211` (the dev project the prior 3 SMOKEs
ran against; preserves continuity with Phase 2's per-resource
hallucination history).

**Expected duration.** ~30-45 minutes for the manual run + retro
write-up.

---

## What Phase 4 changed since SMOKE 3

| Capability | Pre-Phase-4 | Post-Phase-4 |
|---|---|---|
| Detector scope | 2 types (compute_instance, storage_bucket) | 17 types (auto-derived from importer) |
| Drift mode | drift only on the 2 in-scope types | drift-aware on 2; drift-stub on 15 (snapshots+policy run, diff skipped) |
| Unmanaged tracking | none | `Detector.rescan()` returns `DriftReport` with the unmanaged bucket |
| Policy rules | 16 (compute_instance + storage_bucket + AWS pair) | 44 (12 GCP type dirs + AWS pair + common) |
| Policy provenance | none on any rule | three-source (Google-archive + CIS + NIST) on all 44; auto-validated by walker |
| Importer LLM signal | schema + heuristics + scrubbing | + few-shot golden examples for top 10 hallucination-prone types |
| Subprocess timeouts (terraform) | none | per-operation budgets (init=600s, plan=300s, apply=600s, refresh=300s, import=120s, state=60s) |
| Detector hygiene | broad-except + cwd fallback | tightened + PreflightError on missing workdir |
| Policy violation cap | none | per-call=100, per-run=1000 |

---

## Pre-flight (~5 min)

Before starting the manual run, confirm:

1. `python -m pytest detector/tests policy/tests common/tests -q` is green
2. `python -m unittest discover -s importer/tests` is green
3. `python -m unittest discover -s translator/tests` is green
4. `gcloud auth list` shows the right account active
5. `gcloud config get-value project` returns `dev-proj-470211`
   (or `gcloud config set project dev-proj-470211`)
6. `terraform --version` and `conftest --version` both succeed
7. `imported/dev-proj-470211/` workdir is in a known state -- either
   wipe (clean run) or preserve from prior SMOKE (incremental).

For a clean Phase 4 baseline, **wipe** is recommended:
```
rm -rf imported/dev-proj-470211
```

---

## Run script (in order)

### Stage 1: Importer (~10 min)

```
python -m my-terraform-agent.importer.run
```

Select all 17 types when the menu appears (no per-resource
filtering -- we want the full surface).

**What to capture in the retro:**
- `selected` count from the WorkflowResult JSON event
- `imported` count
- `failed` count + per-resource failure_reason if any
- Specifically: did the CC-9 golden examples lift the
  cluster/Cloud Run/etc. first-attempt rate? Compare against
  SMOKE 3's failure list (P2-12, P2-13, P2-14).

**Expectation per CC-9 industry-standard claim:** first-attempt
accuracy on covered types should jump from ~70% (SMOKE 3) to
~90%+ (Phase 4). Specific types to watch:
  * `google_container_cluster` (Autopilot AND Standard variants)
  * `google_cloud_run_v2_service` (P2-12 startup_cpu_boost regress?)
  * `google_container_node_pool` (P2-14 quoted-enum regress?)

### Stage 2: Translator (~5 min)

```
python -m my-terraform-agent.translator.run
```

Pick AWS as target. Then run again with Azure as target. With the
new `run_translation_batch()` from P3-6, multi-file batches should
work via the discover_translatable_files() menu.

**What to capture:**
- Per-target TranslationResult: translated / needs_attention /
  failed counts.
- Any retry/backoff events from the P3-5 `safe_invoke` wrapper
  (`llm_invoke_transient_retry` log lines).

### Stage 3: Detector — drift mode (~5 min)

```
python -m my-terraform-agent.detector.run
```

This exercises the OLD drift-check path on the 2 drift-aware types
PLUS the new drift-stub path on the other 15.

**What to capture:**
- Drift report's resource count: how many drift-aware checks ran
  vs how many drift-stub entries (should be ~2 vs ~15).
- Any false-positive drift on drift-aware types (compute_instance,
  storage_bucket) -- if cloud snapshots vs state are still noisy
  despite P4-PRE's normalization-rule mining, that's a regression
  flag.
- Policy decoration of drift entries -- new types in scope (KMS,
  GKE, etc.) should now show policy_tag values.

### Stage 4: Detector — rescan / unmanaged (~5 min)

This is the NEW capability. Drive programmatically:

```python
from detector.rescan import rescan
from common.workdir import resolve_project_workdir

workdir = resolve_project_workdir("dev-proj-470211", create=False)
report = rescan("dev-proj-470211", project_root=workdir)
print(report.as_fields())
print(f"Unmanaged: {len(report.unmanaged)}")
for cr in report.unmanaged[:10]:
    print(f"  {cr.tf_type}: {cr.cloud_name}")
```

**What to capture:**
- `unmanaged_count` -- if everything in cloud is also in state,
  this should be 0.
- Any `inventory_errors` (per-asset-type enumeration failures).
- If you create a fresh resource in cloud (e.g. `gcloud storage
  buckets create poc-rescan-test`) BEFORE the rescan, it should
  appear in the unmanaged bucket. Manual proof point of CG-1.

### Stage 5: Policy (~5 min)

```
python -m my-terraform-agent.policy.run --project dev-proj-470211
```

This runs all 44 rules across all in-scope resources.

**What to capture:**
- Total violations by severity (HIGH / MED / LOW counts).
- Exit code (0 if no HIGH; non-zero otherwise).
- Spot-check that violation messages carry control IDs (CIS GCP
  X.Y) per the P4-5 deny-message convention.
- Any per-call cap warnings ("Truncated N additional violations")
  -- would flag a buggy rule iterating a long list.

---

## Retro template

After the run, append findings to `docs/smoke4_retro.md` (create from
this template):

```markdown
# SMOKE 4 Retro (Phase 4 closeout)

**Date:** YYYY-MM-DD
**Project:** dev-proj-470211
**Phase 4 commits exercised:** 11a6a85..38aed9e

## Stage 1: Importer
* selected: <N>
* imported: <N>
* failed: <N> (list each + failure_reason)
* CC-9 lift observed: <yes/partial/no>; specific types where
  golden examples helped vs didn't:
    - google_container_cluster (Autopilot): <result>
    - google_container_cluster (Standard): <result>
    - google_cloud_run_v2_service: <result>
    - google_container_node_pool: <result>
    - ...

## Stage 2: Translator
* AWS batch: translated=<N>, needs_attention=<N>, failed=<N>
* Azure batch: translated=<N>, needs_attention=<N>, failed=<N>
* Notable: <transient retry events / Vertex AI quirks / etc.>

## Stage 3: Detector drift
* drift-aware checks: <N>, drift-stub entries: <N>
* False-positive drift: <list>
* Policy decoration on drift: <which tag values appeared>

## Stage 4: Detector rescan
* unmanaged: <N>; sample names: <list>
* inventory_errors: <list>
* Manual unmanaged proof (created bucket -> rescan finds it): <yes/no>

## Stage 5: Policy
* Total violations: HIGH=<N>, MED=<N>, LOW=<N>
* Exit code: <0/1>
* Spot-check: control IDs render in messages: <yes/no>
* Cap warnings: <yes/no; details>

## New issues surfaced (queued for Phase 5 retro or hotfix)
* <bug/issue 1>
* <bug/issue 2>
* ...

## Phase 4 verdict
* CG-1 unmanaged tracking: <ship-ready / needs work / blocked>
* CG-2 policy coverage: <ship-ready / needs work / blocked>
* CG-3 provenance: <ship-ready / needs work / blocked>
* CC-9 few-shot lift: <ship-ready / needs work / blocked>
* Overall Phase 4: <go/no-go for Phase 5>
```

---

## What to do with surfaced bugs

Two paths depending on severity:

**Hotfix in Phase 4** (if it blocks Phase 5):
- Same pattern as P2-12/13/14: add to punchlist as P4-X, ship
  before moving to Phase 5, update SMOKE 4 retro.

**Defer to Phase 5 retro** (if it's a polish issue):
- Add to `docs/saas_readiness_punchlist.md` under "Items
  surfaced by Phase 4 SMOKE".

---

## After the SMOKE: closeout commit

Once the retro is filled in:

```
git add docs/smoke4_retro.md
git commit -m "Phase 4 SMOKE 4 retro: <one-line headline>"
```

Then Phase 4 is officially complete. Tag if desired:
`git tag phase-4-complete`.
