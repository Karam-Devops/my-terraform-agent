# AI-Powered Infrastructure as Code
## Phase 1 & 2 — Complete & Demo Ready
### Automating Cloud Asset Management with Generative AI

---

## Slide 1 — Title

**Title:** AI-Powered Infrastructure as Code — Phase 1 & 2 Complete & Demo Ready
**Subtitle:** Automating Cloud Asset Management with Generative AI
**Presenter:** [Your Name / Title]
**Date:** [Date]
**Status:** Phase 1 ✓ Importer & Translator | Phase 2 ✓ Drift Detection & Policy — Live Demo Ready

---

## Slide 2 — Executive Summary

**Headline:** Two Phases Delivered — Demo Ready Today

**The Challenge**
Managing existing "click-ops" cloud infrastructure is manual, error-prone, and scales poorly. Translating between cloud providers (GCP to AWS/Azure) requires deep, specialised expertise.

**The Solution**
An intelligent, AI-driven agent capable of autonomously reverse-engineering live cloud assets into production-ready, deployable Terraform code — with continuous drift detection and policy enforcement baked in.

**Phase 1 — Completed**
Delivered a robust POC demonstrating automated GCP asset importing and conceptual cloud-to-cloud translation (GCP → AWS / Azure). RAG self-healing architecture delivers zero-drift, validated Terraform on first attempt.

**Phase 2 — Completed & Live Today**
Continuous Drift Detection (Restore / Accept / Drop), Policy-as-Code with per-rule severity, and actionable remediation — all demoed live today.

---

## Slide 3 — Phase 1: AI Importer Engine

**Headline:** From Live Cloud to Perfect Code in Seconds

1. **Automated Discovery** — Instantly scans Google Cloud to identify unmanaged assets across all major services: Compute, Storage, Network, GKE.

2. **LLM-Powered Translation** — Uses Gemini 2.5 Pro to intelligently translate raw Google API JSON into structured HashiCorp Configuration Language (HCL) — multi-file, multi-module.

3. **State Reconciliation** — Automatically executes `terraform import` and `terraform plan` to guarantee the generated code perfectly matches the live cloud environment. Zero Drift.

4. **The Result** — Weeks of manual reverse-engineering reduced to a single, automated command.

**Flow:** GCP Console → Python / AI Agent (Gemini 2.5 Pro + RAG) → main.tf + terraform.tfstate

---

## Slide 4 — The Secret Sauce: RAG & The Learning Loop

**Headline:** Overcoming AI Hallucinations with Deterministic Engineering

**The Problem:** Raw LLMs frequently hallucinate incorrect Terraform syntax or struggle with provider-specific state quirks — making naive generation unreliable for production.

**Pillar 1 — Schema Grounding**
The agent pulls official HashiCorp documentation dynamically, forcing the LLM to adhere to strict, valid provider arguments only.

**Pillar 2 — Heuristics Memory**
A human-in-the-loop training system. When the AI fails a `terraform plan`, an engineer can teach it a "Golden Snippet" or an OMIT/IGNORE rule — stored permanently in `heuristics.json`.

**Pillar 3 — Autonomous Self-Healing**
The agent saves lessons to permanent memory. On subsequent runs, it proactively applies fixes and injects correct patterns — achieving perfect code on Attempt 1.

---

## Slide 5 — Phase 2: Drift Detection Live Demo

**Headline:** Three Real-World Scenarios Demoed Today

**Test 1 — Restore (cloud ← state)**
Storage class was changed outside Terraform. Agent detects the delta, reverts to state-defined config, and validates compliance automatically.
> *"Someone changed our bucket's storage class outside of Terraform. Let's catch it and revert."*

**Test 2 — Accept (cloud → state)**
Cloud value is the correct one — a manual hotfix worth keeping. Agent pulls cloud reality into state and reconciles Terraform.
> *"Sometimes the cloud value is the right one — say a manual hotfix we want to keep. Accept pulls cloud into state."*

**Test 3 — Drop (cloud deleted out-of-band)**
Bucket was deleted directly in the console. State still references a resource that no longer exists. Agent detects the orphan, removes the stale state entry, and triggers remediation.
> *"Someone deleted the bucket directly in the console. State still references something that no longer exists. We need a different flow."*

---

## Slide 6 — Policy as Code Enforcer

**Headline:** Automated Compliance with OPA / Rego — Auditor-Grade Enforcement

**OPA / Rego Standard**
The same policy engine regulators and auditors already trust — no proprietary lock-in, portable across teams and toolchains.

**Severity-Prefix Convention**
Every violation is emitted as `[HIGH][rule_id] message` — parsed by the engine and surfaced in reports with full prioritisation context.

**Filename = Rule ID**
Policy file path resolved automatically. Operators see exactly which file fired — zero ambiguity in audit trails or incident reviews.

**Two Invocation Modes**
Run as a standalone scan against any Terraform plan, or as a drift-decorator layered on top of live drift detection results.

**8 Starter Policies Shipped:**
1. Bucket Encryption — At-rest encryption enforced
2. Public Access Block — No open bucket ACLs
3. Versioning — Object versioning enabled
4. Retention Policy — Minimum retention enforced
5. CMEK Disks — Customer-managed encryption keys
6. No Public IP — Public IP assignment blocked
7. Shielded VM — Secure Boot + vTPM required
8. Mandatory Labels — Cost & ownership tags enforced

---

## Slide 7 — What Was Just Demoed

**Headline:** Four Capabilities in One Unified Platform

**Drift Detection — State vs. Cloud**
Live diff between Terraform state and actual cloud reality. Every delta surfaced instantly — no manual `terraform plan` needed.

**Policy-as-Code with Severity**
Every resource evaluated against Rego policies. Each violation tagged HIGH / MEDIUM / LOW. Not a compliance score — a prioritised, actionable list.

**Drift × Policy in One Pane**
Drift and compliance unified in a single view. Engineers see the state delta and the policy violation in context — no switching between tools.

**Per-Rule Actionable Messages**
Every finding tells you exactly what to fix and how. Not "compliance score: 67%" — specific, rule-level guidance with a remediation path per violation.

**Demonstrated end-to-end:** T1 Restore · T2 Accept · T3 Drop · Policy Enforcement · Unified Single-Pane View

---

## Slide 8 — High-Level Design: Full Architecture

**Headline:** Phase 1 + Phase 2 Unified Engine

**Input Layer**
- gcloud CLI — Asset discovery
- User / YAML Input — Natural language prompt or structured input
- Live Cloud API — Drift source (Phase 2)

**Orchestration Layer**
- Python ThreadPools — Parallel bulk processing
- Drift Scheduler — Continuous diff engine (Phase 2)

**AI / RAG Layer**
- Gemini 2.5 Pro — Code generation
- Docs Scraper — HashiCorp schema RAG
- heuristics.json — Permanent agent memory
- Rego / OPA — Policy-as-Code enforcement (Phase 2)

**Verification Layer**
- terraform.exe — import · plan · validate
- Drift Validator — State vs. Cloud diff (Phase 2)

**Output Layer**
- .tf / .tfstate files
- Drift Report + Policy Violations
- Auto PR (GitOps)

*Note: Phase 2 additions are the Live Cloud API input, Drift Scheduler, Rego/OPA, Drift Validator, and Auto PR output.*

---

## Slide 9 — How the Approaches Compare

**Headline:** A look at how leading IaC automation tools handle key capabilities

*Note: This is a competitive positioning slide. The competitor column refers to Firefly.ai but is labeled "Established Player" in the presentation to avoid naming them explicitly.*

| Capability | Established Player | Our Approach |
|---|---|---|
| **LLM-First Codification Engine** — How live assets are converted to IaC and the role AI plays | Deterministic per-provider mappers as primary engine. LLM used only in a separate AIaC tool — not the core codification path. | LLM-first by design with deterministic guardrails (schema grounding + heuristics) — production-safe output from the core engine. |
| **Drift × Policy Correlation** — Whether drift and policy violations appear in a unified correlated report | Drift on Inventory page, Policy on Governance page — two separate surfaces. Engineers must cross-reference manually. | One report. Each drift line decorated inline with `[⚠️ drift introduces N violation(s)]` — no context switching required. |
| **Cross-Cloud Translation** — Semantic, architecture-aware IaC translation between cloud providers | Cloud Migration feature exists. Internal approach not publicly documented — methodology is a black box. | LLM + Rosetta-stone architectural rules. YAML intermediate representation decouples intent from syntax — traceable, auditable, explainable. |

**Our lead:** ✅ on all three dimensions. Competitor is partial, siloed, or unverified.

---

## Slide 10 — Tier 1: Day 1 Ask

**Headline:** Zero-risk, offline-friendly data requests to accelerate production readiness

| Ask | Why We Need It | Sensitivity |
|---|---|---|
| `gcloud <service> describe ... --format=json` dumps for 10–20 representative resources across the resource types they actually use | Importer schema oracle is only as good as the field shapes it has seen. Real snapshots = real coverage. | LOW — read-only metadata, no secrets |
| A sanitised `terraform.tfstate` (or a redacted excerpt) | Detector + Translator both consume this. Real state has sensitive_attributes, dynamic blocks, modules — all things our 2-resource POC doesn't exercise. | MEDIUM — `terraform state pull \| sanitize` workflow |
| Sample `*.tf` files — 5–10 modules, ideally one per resource type | Validates that our Translator output matches their house style (variable conventions, module structure, locals usage). | LOW — share module skeletons, redact values |
| Their resource-type inventory — "we use these 12 GCP services" | Tells us which Rego policies to prioritise. Stops us from shipping policies for services they don't run. | NONE — no data, just a service list |

---

## Slide 11 — Next Steps & Call to Action

**Headline:** Clear Path to Production

**1. Formal Approval — Phase 2 Production Sign-off**
Endorse the completed Phase 2 capabilities — Drift Detection (Restore / Accept / Drop), Policy-as-Code enforcement, and unified single-pane operations — for progression to client environments.

**2. Tier 1 Data — Client Engagement Prerequisites**
Share the 4 zero-risk data requests outlined in Slide 10. Required to validate coverage against real resource shapes, real state files, and real module conventions before go-live.

**3. Tooling Approvals — Two Service Requests**
- Open Policy Agent (OPA) / tfsec / checkov — Approval to install policy engine executables for automated compliance checks.
- Terraformer / gcloud beta — Read-only permissions to enable the infrastructure-to-code and discovery workflows.

**Closing statement:**
Both phases proven. Engine is live. Ready to onboard the first client environment.
Phase 1 + Phase 2 complete · Drift Detection demoed · Policy enforcement active · GitOps-ready
