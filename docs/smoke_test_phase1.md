# Phase 1 Exit Gate — Importer SMOKE 5/5

**Goal:** verify that every C1-C5 change works on a real `gcloud` /
`terraform` round-trip against the canonical demo project. Five
resource types end-to-end, all green, exit code 0.

**Tester runs this manually.** Output is the gate; no automated
runner exists yet (Phase 5 packaging adds one).

---

## Pre-requisites

| What | How to check |
|---|---|
| `gcloud` authenticated as `poc-sa@<project>.iam.gserviceaccount.com` (or higher) | `gcloud auth list` shows `*` next to the SA |
| `terraform` ≥ 1.5 on PATH | `terraform version` |
| Python venv has `structlog` installed | `python -c "import structlog; print(structlog.__version__)"` |
| Repo at HEAD of `main` (C5 landed) | `git log --oneline -1` shows the C5 commit hash |
| Canonical lock file present | `ls provider_versions/.terraform.lock.hcl` |
| Per-project workdir cleanly initialised, OR fresh | Either delete `imported/<project>/` to test cold start, or leave it for warm-start path |

If your project ID has a GKE node pool, the C5 cluster_flag fix is
exercised in this run. If it doesn't, you can either (a) accept that
the cluster_flag wiring is unverified end-to-end and rely on the AST/
unit-test gate, or (b) create a one-off node pool in `poc-sa` for
this single run and tear it down after.

---

## The five resource types

The current `ASSET_TO_TERRAFORM_MAP` covers ~12 GCP types. For SMOKE
we pick five that exercise different code paths:

| # | Resource type | What it pins | dev-proj-470211 instance |
|---|---|---|---|
| 1 | `google_compute_instance` | The most common type; exercises zone_flag, lifecycle.ignore_changes synthesis, snapshot scrubber | `poc-vm` (us-central1-a) |
| 2 | `google_compute_subnetwork` | Exercises region_flag (regional resource) | `poc-subnet` (us-central1) |
| 3 | `google_storage_bucket` | Exercises name_format (`gs://...` prefix), no location flag | `poc-smoke-bucket-dev-proj-470211` |
| 4 | `google_service_account` | Exercises the email-vs-displayName identity split + account_id injection | `poc-sa@dev-proj-470211.iam.gserviceaccount.com` |
| 5 | `google_container_node_pool` | **Exercises C5 cluster_flag fix end-to-end** | the default node pool of `poc-cluster-std` (us-central1-a) — Standard mode |

Note on GKE coverage: two clusters exist in dev-proj-470211 — `poc-cluster`
(Autopilot, regional, us-central1) and `poc-cluster-std` (Standard, zonal,
us-central1-a). For SMOKE row 5, **pick the node_pool from the Standard
cluster** (`poc-cluster-std`'s default-pool) — Autopilot deliberately
hides its node pools from the Asset API, so they're not selectable.

If you also want to exercise `google_container_cluster` itself (e.g. to
verify the Autopilot mode-detection path in `resource_mode.py`), pick
`poc-cluster` (Autopilot) as a 6th type. Optional bonus coverage.

If your project doesn't have all five, substitute from the supported
set in `importer/config.py::ASSET_TO_TERRAFORM_MAP`. Note which
substitution you made in your test report.

---

## Run

```bash
# From the repo root
python -m my-terraform-agent.importer.run
```

Enter the project ID at the prompt, accept the default if pre-set
via `TARGET_PROJECT_ID`, then select the five resources by number
in the menu.

---

## What "5/5 green" looks like

The final lines printed should match this shape:

```
✅ SUCCESS: <name1>
✅ SUCCESS: <name2>
✅ SUCCESS: <name3>
✅ SUCCESS: <name4>
✅ SUCCESS: <name5>

Summary: 5 / 5 resources imported successfully.
Workflow finished.
```

Followed by ONE structured log line at the end (C3 contract):

```json
{"event": "workflow_complete", "project_id": "...", "selected": 5,
 "imported": 5, "failed": 0, "skipped": 0, "duration_s": ..., ...}
```

And the process exits with `echo $?` returning `0`.

---

## What to verify per commit

Walk through the run output (or the JSON logs if you set
`MTAGENT_LOG_FORMAT=json`) and tick each item.

### C1 — Structured logging
- [ ] Every operational status line is a structured event, not a
      free-text print. Examples to look for: `subprocess_start`,
      `discover_complete`, `describe_complete`, `import_start`,
      `plan_verify_pass`, `kb_load_*`, `rag_mode_activated`,
      `hcl_validation_ok`, `workflow_complete`.
- [ ] Each event has structured fields (`tf_type=...`,
      `resource_name=...`, etc.) — no f-string interpolation.
- [ ] If you set `MTAGENT_LOG_FORMAT=json`, every line parses as
      JSON. (`python -m my-terraform-agent.importer.run 2>&1 |
      python -c "import sys, json; [json.loads(l) for l in
      sys.stdin if l.strip()]"`)

### C2 — Subprocess timeouts
- [ ] `subprocess_start` events include `timeout_s=...`.
- [ ] `terraform_init` events include the per-stage timeout
      (`timeout_s=600`).
- [ ] (Negative test, optional) Set
      `MTAGENT_GCLOUD_TIMEOUT_S=0.001` and re-run a single resource
      — verify the run aborts with `UpstreamTimeout`, the operator
      sees the user_hint, and exit code is 2 (preflight fail) or 1
      (workflow with all failed). Then unset the env var.

### C3 — A+D return contract
- [ ] Final `workflow_complete` log line is present and has the
      six fields documented above.
- [ ] `echo $?` after the run is `0` for the green path.
- [ ] (Negative test, optional) Type a malformed project ID at
      the prompt — verify the run exits with code 2, prints the
      user-hint on stderr (`The workflow could not start because
      the input or environment is invalid...`), and a
      `workflow_preflight_failed` structured log event is emitted.

### C4 — WARN cluster cleanup
- [ ] Run takes visibly less time per resource than before (the
      three `time.sleep(1)` removals save ~3s per resource = ~15s
      across 5).
- [ ] Run the workflow TWICE back-to-back in the same Python
      process (e.g. via a quick wrapper script that calls
      `run_workflow()` twice). The second run's
      `lifecycle.ignore_changes` set must NOT include fields that
      only existed in the first run's resources. (This pins the
      contextvars fix; with the old module-level dict it would
      bleed.)
- [ ] No `time.sleep(1)` noise in the logs. No `[KB] Loading
      schema from ...` prints — those are now DEBUG events under
      `kb_load_start`.

### C5 — cluster_flag wiring
- [ ] If a `google_container_node_pool` is in the selection: the
      `describe_start` event for it should be followed by a
      `describe_complete` (not `subprocess_failed` with
      "Underspecified resource"). The generated `command_args`
      should contain `--cluster <name>`. (Inspect with
      `MTAGENT_LOG_LEVEL=DEBUG` and look at the `subprocess_start`
      event's `cmd` field.)
- [ ] If no node pool available: the AST + unit-test gate is the
      only verification this run can give. Note this caveat in
      your report.

---

## What failure looks like (and what to capture)

If any resource ends up in the failed bucket:

1. The final summary shows `Summary: N / 5 resources imported
   successfully` with N < 5.
2. The interactive correction loop prompts for choice [1] [2]
   [3]. **For the smoke test, use [3] (Skip resource) on every
   failure** — we want to measure the green-path-without-HITL
   behaviour, not the self-correction loop.
3. Exit code will be 1.
4. Capture: the failed resource's `subprocess_failed` event +
   any preceding `subprocess_start` for the same `cmd`, plus the
   final `workflow_complete` line.

Hand the captured events back so we can triage which commit
introduced the regression.

---

## Reporting

A one-liner report is enough:

```
SMOKE result: 5/5 PASS · duration ~Xs · exit 0
Substitutions (if any): <type> -> <type> because <reason>
Optional negative tests: [C2 timeout: PASS/SKIP]
                         [C3 PreflightError: PASS/SKIP]
                         [C5 cluster_flag end-to-end: PASS/SKIP/N-A]
```

If the green path passes, Phase 1 is closed and we move to Phase 2
(resource coverage push).

---

## Phase 2 wave (P2-1..P2-5 verification)

After Phase 2 ships (commits `b156d81` through `0eabf2d`), the
SMOKE re-run covers an expanded set of 17 resource types and
verifies the Phase 2 fixes / additions on top of the Phase 1
hardening. This section layers ON TOP of the Phase 1 procedure
above; the pre-requisites + run + log-grepping mechanics are the
same.

### Phase 2 setup

Phase 2 needs 5 NEW resources in dev-proj-470211 (P2-3 KMS,
P2-4 Cloud Run, P2-5 Pub/Sub). All near-free.

```bash
# One-shot bootstrap of the 5 new types (idempotent):
bash scripts/create_smoke_resources.sh
```

Wait for Asset API indexing (1-5 min) before running the importer.
The script verifies at the end -- if any of the 5 new asset types
isn't listed, wait 60s and re-run just the verify command at the
script's bottom.

### Phase 2 expanded selection

The selection menu now shows **22 supported resources** (12 from
Phase 1 wave + 5 new from Phase 2 + 5 cluster-internal items GKE
auto-creates). Pick all 17 importable ones to verify everything in
one shot:

| Phase | Wave | Type | Resource |
|---|---|---|---|
| 1 | original | google_service_account | `poc-sa` |
| 1 | original | google_compute_subnetwork | `poc-subnet` |
| 1 | original | google_compute_network | `poc-vpc` |
| 1 | original | google_compute_firewall | `poc-fw-allow-icmp` |
| 1 | original | google_compute_disk (standalone) | `poc-disk` |
| 1 | original | google_compute_instance | `poc-vm` |
| 1 | original | google_storage_bucket | `poc-smoke-bucket-...` |
| 1 | original | google_container_cluster (Autopilot) | `poc-cluster` |
| 1 | original | google_container_cluster (Standard) | `poc-cluster-std` |
| 1 | original | google_container_node_pool (Standard) | `default-pool` of poc-cluster-std |
| **2** | **P2-3** | **google_kms_key_ring** | **`poc-keyring`** |
| **2** | **P2-3** | **google_kms_crypto_key** | **`poc-key`** |
| **2** | **P2-4** | **google_cloud_run_v2_service** | **`poc-cloudrun`** |
| **2** | **P2-5** | **google_pubsub_topic** | **`poc-topic`** |
| **2** | **P2-5** | **google_pubsub_subscription** | **`poc-subscription`** |

**Skip** (don't pick — known-bad / known-conflict):
- `google_compute_disk` (boot of poc-vm) — overlap with the VM
- `google_container_node_pool` (Autopilot's default-pool) — Google
  blocks describe
- All `gke-*` firewall rules — GKE-managed, will perpetually drift
- All `gke-*-default-pool-*` Disk/Instance/InstanceTemplate — GKE
  internals
- All 43 `default` per-region subnetworks — noise

### What to verify (delta over Phase 1)

In addition to the Phase 1 checklist, look for these Phase 2 signals:

#### P2-1 (empty-block scrubber)

Look for `post_llm_empty_block_dropped` events in the log. Should
fire 0-N times depending on what the LLM hallucinates. Each fire =
"we caught a hallucination the pre-Phase-2 code would have shipped
to terraform plan-verify".

Search the cluster .tf files after run:
```bash
grep -E "_config \{\s*\}|pubsub \{\s*\}" imported/dev-proj-470211/google_container_cluster_*.tf
```
Should return **NOTHING**. Empty matches = P2-1 working.

#### P2-2 (locations -> node_locations rename)

Look for `post_llm_correction_applied` event with description
`renamed '<root>.locations' -> '<root>.node_locations'`.

Search the cluster .tf files:
```bash
grep -E "^\s*locations\s*=" imported/dev-proj-470211/google_container_cluster_*.tf
```
Should return NOTHING (only `node_locations = ` should appear).

#### P2-3 (CMEK end-to-end)

Look for `discover_complete` events on:
- `cloudkms.googleapis.com/KeyRing` count=1
- `cloudkms.googleapis.com/CryptoKey` count=1

`subprocess_start` for crypto_key describe should include
`--keyring poc-keyring` (the C5 pattern, now mirrored for KMS):
```
gcloud kms keys describe poc-key --project=... --location us-central1 --keyring poc-keyring --format=json
```

Both should reach `hcl_validation_ok` (LLM generates clean HCL for
KMS — small schema, well-known shape).

#### P2-4 (Cloud Run v2)

Look for `discover_complete` on `run.googleapis.com/Service` count=1.

`subprocess_start` for the describe should be:
```
gcloud run services describe poc-cloudrun --project=... --region us-central1 --format=json
```

Cloud Run v2 has a deep nested schema (template > containers >
ports etc.) — the LLM may hallucinate; the HCL gen + plan-verify
flow is the test.

#### P2-5 (Pub/Sub)

Look for `discover_complete` on:
- `pubsub.googleapis.com/Topic` count=1
- `pubsub.googleapis.com/Subscription` count=1

Both describes are flag-less (project-scoped):
```
gcloud pubsub topics describe poc-topic --project=... --format=json
gcloud pubsub subscriptions describe poc-subscription --project=... --format=json
```

Subscription's HCL should reference the topic via a literal
`topic = "projects/dev-proj-470211/topics/poc-topic"` — no
terraform-level dependency, just a string.

### Phase 2 success thresholds

| Metric | Target | Comment |
|---|---|---|
| `workflow_complete` event emitted | YES | Same as Phase 1 |
| Selected count | 15 | (10 Phase 1 + 5 Phase 2 picks above) |
| Imported count | 13-15 | Best case 15/15. Realistic 13/15 if Autopilot pool issues + boot-disk/cluster overlap remain |
| `post_llm_empty_block_dropped` event count | 0-4 | Each fire = P2-1 working |
| `post_llm_correction_applied <root>.locations` | 0-2 | Each fire = P2-2 working (Standard clusters only) |
| Failed cluster cascade | 0-1 | Down from "11 failed (9 sibling-blocked)" pre-Phase-2 |

### Phase 2 cleanup

Same script, now extended to also handle the 5 new types:

```bash
bash scripts/cleanup_smoke_resources.sh
```

Note: KMS crypto keys can't be hard-deleted (Google's design --
24h-30d destruction window). KMS key rings can't be deleted at all.
Both effects are documented in the script; cost while pending is
~$0.06/mo, negligible.
