# Client Failure Modes Matrix (Phase 0 — SaaS Readiness)

**Purpose.** Before a vendor demo on Cloud Run, enumerate the 10 things a
real client will realistically hit in their first session, and specify
what each engine MUST do in response. "Do something reasonable" is not
a spec — each cell below is a concrete, testable contract.

**How to read this.** Columns are the four engines
(Importer / Translator / Detector / Policy). Rows are failure modes. A
cell says what that engine does when the failure occurs *somewhere in
the request chain*, not only when the engine is the direct cause. For
instance, expired credentials hit the Importer first, but the other
engines must still fail cleanly (not crash, not leak stack traces to
the UI) if a cached client somehow makes it past that gate.

**Not a bug list.** These are the paved-road behaviours for Phase 1–4
hardening. Any engine that does not yet meet the spec in a row is a
Phase 1–4 work item; those are tracked separately in the synthesis
doc.

---

## Where this sits in the enterprise pattern

Reference tools (Firefly.ai / Cloud Resilience, ControlMonkey,
Brainboard) all converge on the same 6-step product spine:

    1. Connect cloud account (read-only, no shared creds)
    2. Scan & inventory          -> classify managed vs unmanaged
    3. Codify                    -> turn unmanaged into IaC
    4. Drift detection            -> incl. NEW resources, not just changed ones
    5. Policy gate                -> Rego/OPA over the IaC
    6. Remediation (plan -> apply)

Our 4 engines map onto that spine exactly:

    Importer  -> steps 2 + 3
    Translator -> step 3 (HCL generation, multi-cloud, self-heal)
    Detector  -> step 4
    Policy    -> step 5
    + UI flow  -> steps 1 + 6 (Phase 5 + 6)

The failure modes below are written against this spine, not against
our codebase's current shape. A row that says "detector reports
unmanaged resources" reflects what the *spine* requires, even where
our detector does not yet have that capability — the gap becomes a
work item in the punchlist (`docs/saas_readiness_punchlist.md`),
not a deletion from this doc.

---

## The matrix

| # | Failure mode | Importer | Translator | Detector | Policy |
|---|---|---|---|---|---|
| 1 | **Typo'd project_id** (e.g. `my-projet-470211`) | `ValueError` from `resolve_project_workdir` regex **before** any gcloud call; UI shows "project ID format invalid: must be 6-30 chars, lowercase, start with letter". No workdir created. | N/A (no project_id input — reads from existing workdir). | `ValueError` from same regex; exits before any `terraform` call. | N/A. |
| 2 | **Valid-format project_id but does not exist in GCP** | First `gcloud` list call returns 404/NOT_FOUND. Message: "project 'foo-123456' not found or caller lacks access". Workdir NOT populated; no empty .tf files left behind. | N/A. | `terraform plan` returns provider 403/404; detector surfaces as "target project unreachable — check access" rather than a raw Go stack trace. | N/A. |
| 3 | **Expired / missing ADC credentials** | `gcloud auth` reports RefreshError; message: "run `gcloud auth application-default login` to refresh credentials". Exit 1. No partial .tf files. | Vertex AI init fails at first chat call with RefreshError; message: "translator requires ADC for Vertex AI — run `gcloud auth application-default login`". | `terraform plan` fails; detector surfaces provider auth error unchanged (no retry loop — auth failures are terminal). | N/A (offline — runs conftest only). |
| 4 | **Client lacks IAM roles** on target project (e.g. no `compute.viewer`) | `gcloud` list returns 403 per-resource-type; importer logs "SKIPPED google_compute_instance: 403 Forbidden (compute.viewer missing)" and **continues** with resource types that DO work. Partial import is a valid outcome. | N/A. | Per-resource plan that fails 403 is reported as `drift_status=unknown` with the IAM reason, **not** conflated with real drift. | N/A. |
| 5 | **Unsupported resource type** (client asks for a type not in our handler list) | Importer's dispatch returns "resource type `google_foo_bar` not supported; supported types: [list]". No attempt to shell out to `gcloud`. | Translator accepts any HCL; unsupported types pass through unchanged (not our failure to catch). | Detector's per-target plan works regardless of type; if Terraform itself rejects, the error is surfaced verbatim with the offending type name. | Rego rules that don't match the type simply don't fire; no false-positive violations. |
| 6 | **gcloud CLI not installed or wrong version** on the Cloud Run image | At startup (cold start) `common.gcloud_path.resolve_gcloud()` raises with message "gcloud not found on PATH; set GCLOUD_PATH or bundle the SDK". Fails fast before any request is served. | N/A. | Same cold-start check if detector's workflow calls gcloud (it currently does not — Terraform only). | N/A. |
| 7 | **terraform binary missing or incompatible version** | `common.terraform_path.resolve_terraform()` raises at cold start. Message: "terraform >= 1.6 not found on PATH". | N/A. | Same cold-start check. Plan/apply fails with actionable message if the binary goes missing between cold start and request (edge case). | Conftest is a separate binary; handled in row #8. |
| 8 | **conftest binary missing** (policy engine) | N/A. | N/A. | N/A. | At startup, policy engine checks for conftest; if missing, returns `PolicyImpact(status="unavailable", reason="conftest not installed")` rather than crashing. Detector treats "unavailable" as "no policy blocks", logs a warning, continues. |
| 9 | **Two clients hit the same Cloud Run revision simultaneously** (same project_id by coincidence, or different project_ids) | Workdirs are per-`(tenant, project)`; see `common/workdir.py`. Two concurrent imports into the same workdir **are not supported** and will serialize/corrupt state — Cloud Run max-instances=1 OR a per-workdir lock is required. Documented as a Phase 5 deployment constraint, not a Phase 1–4 fix. | Intermediate YAML files (`_intermediate_blueprint_*.yaml`) are currently written with non-unique names — **collision risk identified in Phase 0 audit**; Phase 3 work item is to make them per-request-id. | Plan runs per-target; concurrent plans inside the same workdir serialize on Terraform's own lock (.terraform.tfstate.lock.info). Safe. | Stateless rego eval; safe under any concurrency. |
| 10 | **Network flake / slow upstream** (Vertex AI 5xx, GCS timeout, terraform registry slow) | `gcloud` calls currently have **no explicit timeout** — Phase 0 audit FAIL; Phase 1 work item. Target: 60s per call, actionable "upstream timed out, retry" message on expiry, no hang. | Vertex AI calls use LangChain's default timeout; Phase 0 audit notes `LLM_TIMEOUT_SECONDS` is defined but not wired to the client. Phase 3 work item. | `terraform plan` has no explicit timeout — Phase 4 work item. Target: 300s per target, cancelled subprocess on expiry. | Conftest has `timeout=30` already — ✅ meets spec. |
| 11 | **Client clicks new resource into the console after baseline** (the "ghost asset" / unmanaged-drift case — Firefly's killer feature) | N/A as a failure mode (importer runs only on demand, by design). The rescan in row 11 / detector reuses the importer's enumeration — see capability gap CG-1. | N/A. | **TODAY: silent miss.** `terraform plan` only sees resources already in state, so a console-clicked bucket is invisible until the next manual codify. **SPEC: Detector returns three buckets** — `drifted` (in state, values differ), `compliant` (in state, matches), `unmanaged` (in cloud, NOT in state). UI surfaces `unmanaged` with a one-click "Codify this" action that hands back to the Importer. Tracked as CG-1 in the punchlist; **Phase 4** capability work. | N/A (policy runs over .tf only; cannot opine on resources that aren't in the IaC). |

---

## Cross-cutting themes pulled out of the matrix

Five themes recur across rows and will be folded into Phase 1–4:

1. **Subprocess timeouts.** Rows #6, #7, #8, #10 all depend on *every*
   external binary call being bounded. Today the Importer and Detector
   fail this. Concrete contract: no `subprocess.run` without
   `timeout=`; a timeout raises a typed exception the engine surfaces
   as a user-facing "upstream slow/unreachable" message, not a 500.

2. **Fail-fast at cold start, not per-request.** Rows #6, #7, #8 are
   deployment-shape failures (binary missing). Cloud Run should refuse
   to serve traffic from a broken image, not surface a runtime crash
   to the first client. Concrete contract: a `preflight()` module that
   runs once at container boot and returns a JSON report; Cloud Run's
   readiness probe reads it.

3. **Errors carry the tenant + project context.** Today log lines are
   bare `print()` calls with no request/tenant/project tags. Rows #2,
   #3, #4 all need the operator debugging a ticket to know *whose*
   import/plan failed. Concrete contract: structured logging
   (JSON-lines to stdout, Cloud Logging auto-parses) with a
   `{tenant_id, project_id, request_id, engine, stage}` context
   dict on every line.

4. **Partial success is a valid outcome.** Rows #4, #5 both require
   the engine to keep going past a single-resource failure rather than
   abort the whole request. Concrete contract: engines return
   structured results (`{imported: [...], skipped: [...], failed: [...]}`)
   rather than raising on the first bad row. Importer and Detector
   currently return `None` or raise — Phase 1 + Phase 4 fixes.

5. **Concurrency boundary is per-workdir, not per-process.** Row #9
   means two concurrent requests for the same `(tenant, project)` tuple
   must serialize somewhere. Cheapest path is Cloud Run `max-instances=1`
   per-client (one revision per tenant) — documented as a Phase 5
   deployment pattern, not an engine-level fix.

---

## Demo-readiness gate

The demo is safe to run when, for every row, the cell's stated behaviour
is **pinned by a test** (unit test for format-level guards, integration
test for subprocess-level behaviour). Phase 4's "full engine smoke"
includes an end-to-end run of each failure mode against the dev
project, asserting the observable behaviour matches this matrix.

Until that gate clears, the matrix IS the spec — not a wish list.
