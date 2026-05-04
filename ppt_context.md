# PPT Rebuild Context — AI-Powered IaC Generation
### Share this entire document with Claude at claude.ai to regenerate the presentation

---

## Presentation Overview

- **Title:** AI-Powered Infrastructure as Code — Phase 1 & 2 Complete & Demo Ready
- **Subtitle:** Automating Cloud Asset Management with Generative AI
- **Total Slides:** 11
- **Style:** Executive dark-theme presentation (dark navy/black backgrounds, Google color palette — #4285F4 blue, #34A853 green, #FBBC04 yellow, #EA4335 red)
- **Format:** Interactive HTML with Prev/Next navigation, keyboard arrow key support, progress bar, and download button
- **Status:** Both Phase 1 and Phase 2 are complete and demoed live

---

## Design System

- **Background colors:** Dark variants — `#0d1117`, `#080e1a`, `#090f1c`, `#070c18`, `#0b1018`, `#060b14`
- **Primary font:** Google Sans / Roboto, sans-serif
- **Card style:** Rounded corners (12px), subtle border `rgba(255,255,255,.07)`, low-opacity colored backgrounds
- **Icons:** Inline SVG, stroke-based, 17–20px, color-matched to card theme
- **Tags/badges:** Pill-shaped, top-right of header, color-coded by status (green = complete/live, yellow = action required, red = urgent)
- **Navigation:** Fixed bottom center nav bar with Prev / Next / counter / Download button
- **Progress bar:** Fixed top, gradient blue→green→yellow, updates per slide

---

## Slide-by-Slide Content

---

### Slide 1 — Title
**Layout:** Full-screen centered, gradient background `#070d1a → #0d1f3c → #0a2744`
**Badge (top right):** `🟢 Phase 1 & 2 Complete · Live Demo Ready` — green

**Content:**
- Eyebrow text: `Platform Engineering · Generative AI`
- H1: `AI-Powered Infrastructure as Code` / `Phase 1 & 2 — Complete & Demo Ready`
- Subtitle: `Automating Cloud Asset Management with Generative AI`
- Two phase badges below title:
  - `✓ Phase 1 — Importer & Translator` (green)
  - `✓ Phase 2 — Drift Detection & Policy` (blue)
- Three info pills: `Presented by [Your Name / Title]` · `Date [Date]` · `Classification Internal`

---

### Slide 2 — Executive Summary
**Header icon:** Document icon, blue
**Tag:** `✓ Both Phases Complete` — green
**Subheading:** Two Phases Delivered — Demo Ready Today

**Layout:** 2×2 card grid

**Cards:**
1. **The Challenge** (red tint) — Managing click-ops cloud infrastructure is manual, error-prone, and scales poorly. Translating between clouds requires deep specialised expertise.
2. **The Solution** (blue tint) — An intelligent AI agent that autonomously reverse-engineers live cloud assets into production-ready Terraform — with continuous drift detection and policy enforcement.
3. **Phase 1 ✓ — Importer & Translator** (green tint) — Automated GCP asset importing + GCP→AWS/Azure translation. RAG self-healing delivers zero-drift, validated Terraform on first attempt.
4. **Phase 2 ✓ — Drift & Policy — Live Today** (yellow tint) — Continuous Drift Detection (Restore / Accept / Drop), Policy-as-Code with per-rule severity, and actionable remediation — demoed live today.

---

### Slide 3 — Phase 1 — AI Importer Engine
**Header icon:** Upload/import arrow, green
**Tag:** `✓ Complete` — green
**Subheading:** From Live Cloud to Perfect Code in Seconds

**Layout:** Split — feature list left, SVG flow diagram right

**Feature list (4 items):**
1. **Automated Discovery** (blue number) — Scans GCP to identify unmanaged assets across Compute, Storage, Network, and GKE instantly.
2. **LLM-Powered Translation** (yellow number) — Gemini 2.5 Pro converts raw Google API JSON into structured, valid HCL — multi-file, multi-module.
3. **State Reconciliation** (purple number) — Runs `terraform import` + `terraform plan` to guarantee Zero Drift between generated code and live environment.
4. **The Result** (green checkmark, green tint card) — Weeks of manual reverse-engineering reduced to a **single automated command.**

**SVG Flow Diagram (right side):**
- Box 1: `GCP Console` → "Live unmanaged assets" (blue border)
- Arrow down labeled `gcloud scan`
- Box 2: `Python / AI Agent` → "Gemini 2.5 Pro + RAG" → "JSON → HCL translation" (yellow border)
- Arrow down
- Two output boxes side by side:
  - `main.tf` — Valid HCL (green border)
  - `terraform.tfstate` — Zero Drift (green border)
- Final banner: `✓ Production-Ready Output` (green)

---

### Slide 4 — The Secret Sauce — RAG & The Learning Loop
**Header icon:** Lightbulb, yellow
**Tag:** `Core Innovation` — yellow
**Subheading:** Overcoming AI Hallucinations with Deterministic Engineering

**Layout:** Problem banner on top, 3-pillar card grid below

**Problem banner (red tint):**
> Raw LLMs hallucinate incorrect Terraform syntax — making naive generation unreliable for production.

**3 Pillar cards (each with colored top bar and large faded number watermark):**

1. **Pillar 1 — Schema Grounding** (blue, top bar #4285F4)
   - Icon: Book
   - Pulls official HashiCorp docs dynamically, forcing the LLM to adhere to strict, valid provider arguments only.

2. **Pillar 2 — Heuristics Memory** (green, top bar #34A853)
   - Icon: People/team
   - Human-in-the-loop training. Engineers teach Golden Snippets and OMIT rules saved to `heuristics.json`.

3. **Pillar 3 — Autonomous Self-Healing** (yellow, top bar #FBBC04)
   - Icon: Refresh/loop
   - Fixes applied proactively on subsequent runs — achieving perfect code on **Attempt 1.**

---

### Slide 5 — Phase 2 — Drift Detection Live Demo
**Header icon:** Eye, blue
**Tag:** `🟢 Live Demo Ready` — green
**Subheading:** Three Real-World Scenarios Demoed Today

**Layout:** 3 stacked test cards, each with a side action badge

**Test cards:**

**T1 — Restore** `cloud ← state` (blue tint, REVERT badge)
- Storage class changed outside Terraform — agent detects the delta, reverts to state-defined config, and validates compliance automatically.
- Narration (italic): *"Someone changed our bucket's storage class outside of Terraform. Let's catch it and revert."*

**T2 — Accept** `cloud → state` (yellow tint, ACCEPT badge)
- Cloud value is correct — a manual hotfix worth keeping. Agent pulls cloud reality into state and reconciles Terraform.
- Narration (italic): *"Sometimes the cloud value is the right one — say a manual hotfix we want to keep. Accept pulls cloud into state."*

**T3 — Drop** `cloud deleted out-of-band` (red tint, DROP badge)
- Bucket deleted directly in console. State references a non-existent resource. Agent detects the orphan and triggers remediation.
- Narration (italic): *"Someone deleted the bucket directly in the console. State still references something that no longer exists. We need a different flow."*

---

### Slide 6 — Policy as Code Enforcer
**Header icon:** Shield with checkmark, red
**Tag:** `🟢 Live Today` — green
**Subheading:** Automated Compliance with OPA / Rego — Auditor-Grade Enforcement

**Layout:** Split — 4 feature items left, 8 policy cards (2×4 grid) right

**Left — 4 Feature Items:**

1. **OPA / Rego Standard** (blue icon: layers/stack)
   - The same policy engine regulators and auditors already trust — no proprietary lock-in, portable across teams and toolchains.

2. **Severity-Prefix Convention** (red icon: tag)
   - Every violation emitted as `[HIGH][rule_id] message` — parsed and surfaced in reports with full prioritisation.

3. **Filename = Rule ID** (yellow icon: list/lines)
   - Policy file path resolved automatically. Operators see exactly which file fired — zero ambiguity in audit trails.

4. **Two Invocation Modes** (purple icon: terminal/code)
   - **Standalone scan** against any Terraform plan, or as a **drift-decorator** layered on live drift detection results.

**Right — 8 Starter Policies (2×4 grid):**

| Policy | Color | Description |
|---|---|---|
| Bucket Encryption | Red | At-rest encryption enforced |
| Public Access Block | Red | No open bucket ACLs |
| Versioning | Yellow | Object versioning enabled |
| Retention Policy | Yellow | Minimum retention enforced |
| CMEK Disks | Blue | Customer-managed encryption |
| No Public IP | Blue | Public IP assignment blocked |
| Shielded VM | Green | Secure Boot + vTPM required |
| Mandatory Labels | Green | Cost & ownership tags enforced |

Each policy card has a matching SVG icon and colored border.

---

### Slide 7 — What Was Just Demoed
**Header icon:** Play circle, green
**Tag:** `Demo Recap` — green
**Subheading:** Four Capabilities in One Unified Platform

**Layout:** 2×2 card grid + summary bar at bottom

**4 Cards:**
1. 🔍 **Drift Detection — State vs. Cloud** (blue tint) — Live diff between Terraform state and actual cloud reality. Every delta surfaced instantly — no manual `terraform plan` needed.
2. 🛡️ **Policy-as-Code with Severity** (red tint) — Every resource evaluated against Rego policies — each violation tagged `HIGH` / `MEDIUM` / `LOW`. Not a score — a prioritised action list.
3. 📊 **Drift × Policy in One Pane** (yellow tint) — Drift and compliance unified in a single view — engineers see the state delta and policy violation in context, no tool switching.
4. ✅ **Per-Rule Actionable Messages** (green tint) — Every finding tells you exactly what to fix and how. Not *"compliance score: 67%"* — specific rule-level guidance with a remediation path per violation.

**Bottom summary bar (green tint):**
> Demonstrated end-to-end: **T1 Restore** · **T2 Accept** · **T3 Drop** · **Policy Enforcement** · **Unified Single-Pane View**

---

### Slide 8 — High-Level Design — Full Architecture
**Header icon:** 4-square grid, blue
**Tag:** `HLD` — blue
**Subheading:** Phase 1 + Phase 2 Unified Engine

**Layout:** Full-width SVG architectural block diagram with 5 horizontal layers

**5 Layers (top to bottom), each in a dashed container:**

1. **INPUT** (blue dashed border)
   - `gcloud CLI` — Asset discovery (solid blue box)
   - `User / YAML Input` — Prompt or structured input (solid blue box)
   - `Live Cloud API` — Drift source / Phase 2 (dashed blue box — Phase 2 addition)

2. **ORCHESTRATION** (purple dashed border)
   - `Python ThreadPools` — Parallel bulk processing (solid purple box)
   - `Drift Scheduler` — Continuous diff engine / Phase 2 (dashed purple box — Phase 2 addition)

3. **AI / RAG LAYER** (yellow dashed border)
   - `Gemini 2.5 Pro` — Code generation (solid yellow box)
   - `Docs Scraper` — HashiCorp schema RAG (solid yellow box)
   - `heuristics.json` — Permanent memory (solid yellow box)
   - `Rego / OPA` — Policy-as-Code / Phase 2 (dashed red box — Phase 2 addition)

4. **VERIFICATION** (red dashed border)
   - `terraform.exe` — import · plan · validate (solid red box)
   - `Drift Validator` — State vs. Cloud diff / Phase 2 (dashed red box — Phase 2 addition)

5. **OUTPUT** (green solid bar)
   - `.tf / .tfstate files` · `Drift Report + Policy Violations` · `Auto PR (GitOps)`

**Phase 2 additions** are distinguished with dashed borders throughout.
A vertical label on the right reads: `✦ PHASE 2 ADDITIONS`
Arrows connect layers top-to-bottom using blue (down) and green (final output) arrow markers.

---

### Slide 9 — How the Approaches Compare
**Header icon:** Bar chart, green
**Tag:** `Capability Analysis` — green
**Subheading:** A look at how leading IaC automation tools handle key capabilities

**Note to Claude:** This is a competitive positioning slide comparing our tool against a well-known IaC automation competitor (Firefly.ai) without naming them explicitly. The competitor column is labeled **"Established Player"** with subtitle "Cloud governance tooling". Our column is labeled **"Our Approach"** with subtitle "AI-first, deterministic guardrails".

**Layout:** 3-column matrix (Capability | Established Player | Our Approach) × 3 rows

**Column headers:**
- Col 1: `Capability` (plain label)
- Col 2: `Established Player` — dark card, neutral grey icon, subtitle "Cloud governance tooling"
- Col 3: `Our Approach` — green tint card, green checkmark icon, subtitle "AI-first, deterministic guardrails"

**Row 1 — LLM-First Codification** (blue icon: code brackets)
- Description: How live assets are converted to IaC — and the role AI plays in that process.
- Established Player 🔶 Partial: Deterministic per-provider mappers as primary engine. LLM used only in a separate AIaC tool — not the core codification path.
- Our Approach ✅ Native Advantage: LLM-first by design, with deterministic guardrails (schema grounding + heuristics) ensuring production-safe output from the core engine.

**Row 2 — Drift × Policy Correlation** (red icon: analytics/report)
- Description: Whether drift findings and policy violations appear in a unified, correlated report.
- Established Player 🔶 Siloed: Drift lives on the Inventory page. Policy sits on the Governance page. Two separate surfaces — engineers must cross-reference manually.
- Our Approach ✅ Unified View: One report. Each drift line is decorated inline with `[⚠️ drift introduces N violation(s)]` — no context switching required.

**Row 3 — Cross-Cloud Translation** (yellow icon: left-right arrows)
- Description: Semantic, architecture-aware translation of IaC between cloud providers.
- Established Player 🔷 Unverified: Cloud Migration feature exists. Internal approach not publicly documented — translation methodology is a black box.
- Our Approach ✅ Transparent Architecture: LLM + Rosetta-stone architectural rules. YAML intermediate representation decouples intent from syntax — traceable, auditable, explainable.

---

### Slide 10 — Tier 1 — Day 1 Ask
**Header icon:** Clipboard, yellow
**Tag:** `Action Required` — yellow
**Subheading:** Zero-risk, offline-friendly data requests to accelerate production readiness

**Layout:** Full-width executive table with 3 columns

**Table columns:** Ask | Why We Need It | Sensitivity

**4 Rows:**

| Ask | Why We Need It | Sensitivity |
|---|---|---|
| `gcloud <service> describe ... --format=json` dumps for 10–20 representative resources across resource types in use | Importer schema oracle is only as good as the field shapes it has seen. Real snapshots = real coverage. | 🟢 LOW — Read-only metadata, no secrets |
| Sanitised `terraform.tfstate` (or a redacted excerpt) | Detector + Translator both consume this. Real state has `sensitive_attributes`, dynamic blocks, modules — all things our 2-resource POC doesn't exercise. | 🟡 MEDIUM — `terraform state pull \| sanitize` workflow |
| Sample `*.tf` files — 5–10 modules, one per resource type | Validates our Translator output matches house style (variable conventions, module structure, locals usage). | 🟢 LOW — Share module skeletons, redact values |
| Resource-type inventory — "we use these 12 GCP services" | Tells us which Rego policies to prioritise. Stops us shipping policies for services they don't run. | 🟢 NONE — No data, just a service list |

Sensitivity column uses colored pill badges: 🟢 LOW (green), 🟡 MEDIUM (yellow).

---

### Slide 11 — Next Steps & Call to Action
**Header icon:** Checklist, green
**Tag:** `Action Required` — red
**Subheading:** Clear Path to Production

**Layout:** 3 stacked action cards + closing CTA banner

**Action cards:**

1. **Formal Approval — Phase 2 Production Sign-off** (blue tint, numbered circle `1`)
   - Endorse the completed Phase 2 capabilities — Drift Detection (Restore / Accept / Drop), Policy-as-Code enforcement, and unified single-pane operations — for progression to client environments.

2. **Tier 1 Data — Client Engagement Prerequisites** (yellow tint, numbered circle `2`)
   - Share the 4 zero-risk data requests from the previous slide — required to validate coverage against real resource shapes, state files, and module conventions before go-live.

3. **Tooling Approvals — Two Service Requests** (red tint, numbered circle `3`)
   - Pill tags:
     - 🛡️ **OPA / tfsec / checkov** — Policy engine install
     - ☁️ **Terraformer / gcloud beta** — Read-only access

**Closing CTA banner (gradient blue→green):**
> 🚀 **Both phases proven. Engine is live. Ready to onboard the first client environment.**
> Phase 1 + Phase 2 complete · Drift Detection demoed · Policy enforcement active · GitOps-ready

---

## Prompt to give Claude

Paste the entire document above and use this prompt:

> "Using the slide-by-slide context below, build me an 11-slide executive-grade interactive HTML presentation. Use a dark theme with Google color palette (blue #4285F4, green #34A853, yellow #FBBC04, red #EA4335). Each slide should use the layout described. Include a fixed bottom navigation bar with Prev / Next buttons, slide counter, and a Download button. Add a top progress bar. Use inline SVG icons throughout. Make it polished and professional."
