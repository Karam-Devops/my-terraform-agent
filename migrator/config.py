"""Migrator engine configuration.

Env vars + defaults consumed across the engine. Mirrors translator.config
+ detector.config patterns.
"""

import os
from typing import List


# Source-cloud allowlist. Today only GCP. Future: azure_*.
MIGRATOR_SOURCE_CLOUDS = ["gcp"]

# Target-cloud allowlist. Today AWS only.
MIGRATOR_TARGETS_ALLOWED = [
    t.strip().lower()
    for t in os.environ.get("MIGRATOR_TARGETS_ALLOWED", "aws").split(",")
    if t.strip()
]

# Per-run violation cap (defensive — for the rare malicious-input case
# where a repo has 50k resources and the customer wants to translate
# them all).
MIGRATOR_MAX_RESOURCES_PER_RUN = int(
    os.environ.get("MIGRATOR_MAX_RESOURCES_PER_RUN", "5000")
)

# Where translation outputs land in the per-project workdir.
MIGRATOR_OUTPUT_DIRNAME = "migrator_output"

# File extensions we recognize as IaC source.
MIGRATOR_SOURCE_EXTENSIONS = (".tf", ".hcl", ".tfvars")

# Terragrunt's marker filename (presence anywhere in the tree =
# Terragrunt repo, otherwise vanilla Terraform).
TERRAGRUNT_MARKER = "terragrunt.hcl"


def is_target_allowed(target: str) -> bool:
    """True iff `target` is in the env-controlled target allowlist."""
    return target.strip().lower() in MIGRATOR_TARGETS_ALLOWED
