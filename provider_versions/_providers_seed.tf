# provider_versions/_providers_seed.tf
#
# Minimal Terraform provider declaration, copied (seeded) into every
# fresh per-project workdir alongside `.terraform.lock.hcl` BEFORE the
# importer's first `terraform init` runs.
#
# WHY THIS FILE EXISTS (D-6 fix, 2026-04-28)
# ------------------------------------------
# The importer's preflight in `importer/run.py` calls `terraform init`
# in the per-project workdir as soon as the workdir is resolved -- that
# is, BEFORE Stage 3 (HCL generation) creates any `.tf` files. With NO
# `.tf` files declaring providers, `terraform init` happily creates
# `.terraform/` but does NOT download any providers (it has nothing to
# install).
#
# Then Stage 3 begins HCL generation. The very first thing it does is
# load the per-resource Terraform schema via `importer.knowledge_base`,
# which (on a cache miss) tries to bootstrap from
# `terraform providers schema -json`. That subprocess returns an empty
# schema -- the providers were never installed -- so the bootstrap
# fails for every resource type. The LLM then operates in
# `no_context_mode` (no schema grounding), which is the documented
# failure mode for hallucinated fields like `consume_reservation_type`,
# unsupported `enable_gvnic` blocks on clusters, etc.
#
# By seeding this minimal `terraform { required_providers { ... } }`
# block alongside the lock file, the FIRST `terraform init` actually
# pulls the google provider into `.terraform/`. The schema query later
# succeeds, the KB cache populates correctly, and the LLM gets proper
# grounding from the very first HCL generation attempt.
#
# Once the importer generates real `.tf` files (Stage 3+), they will
# only contain `resource "..." { ... }` blocks -- no second
# `terraform { ... }` block -- so this seed file remains the sole
# source of provider declaration for the workdir. Operator-edited
# workdirs that add their own `terraform { ... }` block in another
# `.tf` file are fine: Terraform allows multiple `terraform` blocks
# across files and merges them, as long as the version constraints are
# compatible.
#
# VERSION ALIGNMENT
# -----------------
# The version pin below MUST match `.terraform.lock.hcl` exactly. The
# lock pins the immutable hash; this file pins the source-and-version
# constraint that `terraform init` resolves AGAINST that lock.
#
# To bump the provider version:
#   1. Edit BOTH this file's `version =` AND the version line in
#      `.terraform.lock.hcl` in the same commit.
#   2. OR (recommended): in any per-project workdir, run
#         terraform init -upgrade
#      then copy the regenerated `.terraform.lock.hcl` and a fresh
#      version pin from this file BACK into `provider_versions/`.
#
# Naming: leading underscore (`_providers_seed.tf`) signals this is a
# tooling artifact, not customer-edited HCL. Operators who delete it
# will get the pre-D-6 broken behavior on the next fresh-workdir
# import; we accept this as deliberate-override semantics.

terraform {
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "7.29.0"
    }
  }
}
