"""Walk a repository's filesystem and classify it.

Deterministic, no I/O beyond stat + readdir. Tells the rest of the
ingest pipeline whether to treat the repo as Terragrunt-shaped (presence
of terragrunt.hcl files) or vanilla Terraform.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import List

from migrator.config import (
    MIGRATOR_SOURCE_EXTENSIONS,
    TERRAGRUNT_MARKER,
)


# Directories we never descend into — git metadata, terraform caches,
# IDE files, etc. Pulled out as a frozen set so the walker stays
# allocation-light on large repos.
_SKIP_DIRS = frozenset({
    ".git",
    ".terraform",
    ".terragrunt-cache",
    "__pycache__",
    ".idea",
    ".vscode",
    "node_modules",
    # Migrator's own output directory — never re-scan our own output
    # from a previous run (prevents circular re-translation).
    "migrator_output",
})


@dataclass
class WalkResult:
    repo_root: str
    source_iac: str               # "terraform" | "terragrunt"
    tf_files: List[str]           # absolute paths of .tf
    terragrunt_files: List[str]   # absolute paths of terragrunt.hcl
    tfvars_files: List[str]       # absolute paths of *.tfvars
    other_hcl_files: List[str]    # absolute paths of non-terragrunt .hcl

    @property
    def total_files(self) -> int:
        return (
            len(self.tf_files)
            + len(self.terragrunt_files)
            + len(self.tfvars_files)
            + len(self.other_hcl_files)
        )


def walk_repo(repo_root: str) -> WalkResult:
    """Walk `repo_root` and bucket every IaC file by kind.

    Returns a WalkResult; raises FileNotFoundError if `repo_root` doesn't
    exist (caller's preflight should catch this earlier).
    """
    if not os.path.isdir(repo_root):
        raise FileNotFoundError(f"repo_root does not exist: {repo_root}")

    tf_files: List[str] = []
    terragrunt_files: List[str] = []
    tfvars_files: List[str] = []
    other_hcl_files: List[str] = []

    for dirpath, dirnames, filenames in os.walk(repo_root):
        # Prune in-place to stop os.walk from descending.
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]

        for fname in filenames:
            full = os.path.join(dirpath, fname)
            if fname == TERRAGRUNT_MARKER:
                terragrunt_files.append(full)
            elif fname.endswith(".tf"):
                tf_files.append(full)
            elif fname.endswith(".tfvars") or fname.endswith(".tfvars.json"):
                tfvars_files.append(full)
            elif fname.endswith(".hcl"):
                other_hcl_files.append(full)

    source_iac = "terragrunt" if terragrunt_files else "terraform"

    # Stable ordering for deterministic downstream output.
    tf_files.sort()
    terragrunt_files.sort()
    tfvars_files.sort()
    other_hcl_files.sort()

    return WalkResult(
        repo_root=os.path.abspath(repo_root),
        source_iac=source_iac,
        tf_files=tf_files,
        terragrunt_files=terragrunt_files,
        tfvars_files=tfvars_files,
        other_hcl_files=other_hcl_files,
    )
