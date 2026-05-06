"""Migrator engine result dataclasses.

MigrationResult is the public return type of run_migration. Mirrors the
shape conventions of importer.results.WorkflowResult and
translator.results.TranslationResult so the Dashboard can render any
engine's snapshot uniformly.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


# Confidence band labels, used both for per-resource scoring and
# for renderable summary counts.
CONFIDENCE_HIGH = "HIGH"
CONFIDENCE_MEDIUM = "MEDIUM"
CONFIDENCE_LOW = "LOW"
CONFIDENCE_MANUAL = "MANUAL_REVIEW"

CONFIDENCE_BANDS = (
    CONFIDENCE_HIGH,
    CONFIDENCE_MEDIUM,
    CONFIDENCE_LOW,
    CONFIDENCE_MANUAL,
)


@dataclass
class DiscoveredResource:
    """One resource declared in the customer's source repo.

    The `address` is the canonical Terraform reference: `<tf_type>.<name>`
    inside its module. The `module_path` is the relative path of the
    file/module that declared it.
    """
    tf_type: str                      # e.g. "google_compute_network"
    name: str                         # the HCL resource label
    module_path: str                  # path within repo (e.g. "modules/networking")
    file_path: str                    # absolute or repo-relative file path
    arguments: Dict[str, Any] = field(default_factory=dict)

    @property
    def address(self) -> str:
        return f"{self.tf_type}.{self.name}"


@dataclass
class ConfidenceFinding:
    """Per-resource confidence assessment + AWS mapping.

    `aws_equivalent` is None when the resource has no AWS analog
    (band == MANUAL_REVIEW).
    """
    resource_address: str
    tf_type: str
    band: str                         # one of CONFIDENCE_BANDS
    score_pct: int                    # 0–100
    aws_equivalent: Optional[str]     # e.g. "aws_vpc", or None
    reason: str                       # one-line operator-facing explanation
    notes: List[str] = field(default_factory=list)  # caveats, gaps


@dataclass
class DependencyEdge:
    """One directed edge in the resource dep graph."""
    source: str   # resource address that depends on...
    target: str   # ...this resource address
    via: str      # the attribute path (e.g. ".id", ".self_link")


@dataclass
class MigrationResult:
    """End-to-end Migrator output.

    A+D contract: returned regardless of per-resource outcomes. Empty
    fields indicate "phase ran cleanly but found nothing"; failures
    surface in `errors` (best-effort — engine never raises after a
    successful preflight).
    """
    project_id: Optional[str]
    repo_path: str
    target_cloud: str
    source_iac: str                              # "terraform" | "terragrunt"

    # Discover phase
    resources: List[DiscoveredResource] = field(default_factory=list)
    files_scanned: int = 0

    # Plan phase
    dep_edges: List[DependencyEdge] = field(default_factory=list)
    confidence: List[ConfidenceFinding] = field(default_factory=list)

    # Generate phase
    output_dir: Optional[str] = None
    migration_guide_path: Optional[str] = None
    helper_script_paths: List[str] = field(default_factory=list)
    skeleton_paths: List[str] = field(default_factory=list)

    # Bookkeeping
    duration_s: float = 0.0
    errors: List[str] = field(default_factory=list)

    # ---- summary helpers (used by UI + Dashboard snapshot) ----

    @property
    def confidence_summary(self) -> Dict[str, int]:
        """Counts per band for hero-metric rendering."""
        out = {b: 0 for b in CONFIDENCE_BANDS}
        for c in self.confidence:
            if c.band in out:
                out[c.band] += 1
        return out

    @property
    def resource_count(self) -> int:
        return len(self.resources)

    @property
    def exit_code(self) -> int:
        """0 = no preflight errors. CLI exit code parity with other engines."""
        return 1 if self.errors else 0

    def as_fields(self) -> Dict[str, Any]:
        """Flat-dict shape for snapshots + structured logging."""
        return {
            "project_id": self.project_id or "unknown",
            "repo_path": self.repo_path,
            "target_cloud": self.target_cloud,
            "source_iac": self.source_iac,
            "files_scanned": self.files_scanned,
            "resource_count": self.resource_count,
            "dep_edge_count": len(self.dep_edges),
            "confidence_summary": self.confidence_summary,
            "output_dir": self.output_dir or "",
            "migration_guide_path": self.migration_guide_path or "",
            "helper_script_count": len(self.helper_script_paths),
            "skeleton_path_count": len(self.skeleton_paths),
            "duration_s": self.duration_s,
            "errors": self.errors,
            "exit_code": self.exit_code,
        }
