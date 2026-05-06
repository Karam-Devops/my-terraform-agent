"""Migrator validation layer.

Tiered validation of emitted Terragrunt output. Tiers 0–3 are
zero-cost (no cloud creds required) and run automatically after
emission. Tiers 4–6 (deferred to v2) need AWS sandbox credentials.

  Tier 0: HCL parses                — native (python-hcl2 in ingest)
  Tier 1: terragrunt hclfmt --check — format conformance
  Tier 2: terragrunt hclvalidate    — Terragrunt-specific HCL semantics
  Tier 3: terragrunt validate-inputs — input/variable contract per stack

Tiers 4–6 stub for v2:
  Tier 4: terragrunt run-all validate — provider schema check
  Tier 5: terragrunt run-all plan    — full plan against real AWS
  Tier 6: apply-and-verify on sandbox — end-to-end
"""

from .terragrunt_validator import (
    is_terragrunt_available,
    validate_target,
)

__all__ = ["is_terragrunt_available", "validate_target"]
