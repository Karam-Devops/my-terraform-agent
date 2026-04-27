# Few-shot Golden Examples for HCL Generation (CC-9)

**What.** Hand-written, plan-clean HCL for the importer's most
hallucination-prone resource types. The LLM pattern-matches against
the example to produce correct output, instead of working from
schema + JSON alone.

**Why this approach.** Phase 2 SMOKEs surfaced a long tail of LLM
hallucinations:
  * Field-name confusion (`cluster_ipv4_cidr` vs `cluster_ipv4_cidr_block`)
  * Nesting mistakes (`cgroup_mode` placed in wrong block)
  * v1 vs v2 API confusion (Cloud Run `container_concurrency`
    appearing on a v2 service)
  * Boolean-vs-quoted-enum coercion bugs (P2-14
    `insecure_kubelet_readonly_port_enabled`)

Per published industry results (Anthropic, GitHub Copilot, Cursor),
few-shot prompting with golden examples typically lifts first-attempt
accuracy from ~70% to ~90%+ on covered types -- without architectural
changes (constrained generation / function calling) that would take
weeks.

**Filename convention.**
  * `<tf_type>.tf` -- default example for the type.
  * `<tf_type>__<mode_id>.tf` -- mode-specialized variant. Mode IDs
    come from `importer/resource_mode.py:detect_modes()` (e.g.
    `gke_autopilot`, `gke_standard`).

The loader (`importer/golden_examples_loader.py`) tries
mode-specialized variants first, falls back to the default.

**What each example demonstrates.**
  * Correct field names + nesting (positive example).
  * Absence of v1-vestige / Autopilot-managed / quoted-enum-mistyped
    fields (negative example: the LLM pattern-matches against a
    "this is what right looks like" reference instead of guessing
    from schema text).
  * Plan-clean against a canonical instance of the type.

**Adding new examples.** For each new type:
  1. Run the importer against a representative resource of that
     type and identify the recurring hallucination class.
  2. Hand-write a minimal, plan-clean HCL example that
     demonstrates the correct shape (and excludes the
     hallucination-prone fields).
  3. Drop into this directory with the right filename.
  4. Verify with `terraform plan` against a real instance.
  5. Update `docs/policy_provenance.md` if the rule's mining map
     references it.

**Phase 4 P4-9a / P4-9b coverage:**
  * P4-9a (this commit): cluster Autopilot, cluster Standard,
    Cloud Run v2 (the 3 with concrete Phase 2 SMOKE evidence).
  * P4-9b (next commit): node_pool, compute_instance,
    storage_bucket, kms_crypto_key, pubsub_subscription,
    compute_subnetwork, service_account.
