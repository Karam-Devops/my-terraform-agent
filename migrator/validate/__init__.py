"""Migrator validation layer.

Two validators, picked at runtime by source_iac:

  * terragrunt_validator — runs `terragrunt hcl format/validate`
    against an emitted Terragrunt target tree. Used when source_iac
    is "terragrunt".

  * terraform_validator — runs `terraform fmt/init/validate` against
    an emitted pure-Terraform target tree. Used when source_iac is
    "terraform".

Both share the TierResult / ValidationReport shape so the UI surfaces
them through one rendering path.

Tiers 0-2 are zero-cost (no cloud creds). Tiers 4-6 (real apply)
deferred to v2.
"""

from .terragrunt_validator import (
    TierResult,
    ValidationReport,
    is_terragrunt_available,
    validate_target,
)
from .terraform_validator import (
    is_terraform_available,
    validate_target as validate_terraform_target,
)

# Default `validate_target` continues to point at the terragrunt validator
# (preserves prior callers of `migrator.validate.validate_target`).
__all__ = [
    "TierResult",
    "ValidationReport",
    "is_terragrunt_available",
    "is_terraform_available",
    "validate_target",
    "validate_terraform_target",
]
