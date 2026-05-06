"""Tiers 0–2 validation of an emitted target/ tree.

Each tier returns a ValidationTierResult; the orchestrator collects
them into a ValidationReport. UI renders this report in the
Validation tab.

Tier 0 is always run (uses python-hcl2 from ingest — already a
dependency). Tiers 1–2 require the `terragrunt` CLI on PATH; if
not present, those tiers report `available=False` with a one-line
install hint.

Note: terragrunt 1.0+ redesigned the CLI:
  * legacy `hclfmt` → `hcl format`
  * legacy `hclvalidate` → `hcl validate`
  * legacy `validate-inputs` → removed entirely (replaced by
    `terragrunt run validate` per-stack which we don't run because
    it requires the module source to be reachable + cloud creds)

This validator targets terragrunt 1.0+ syntax. Tier 3 (legacy
validate-inputs) is dropped from the active tier set.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
from dataclasses import dataclass, field
from typing import List, Optional

logger = logging.getLogger(__name__)


@dataclass
class TierResult:
    tier: int
    name: str
    available: bool                # True iff the tooling required for this tier is present
    passed: bool                   # True iff the check passed (or N/A when unavailable)
    duration_s: float = 0.0
    files_checked: int = 0
    failures: List[str] = field(default_factory=list)   # one-line summaries
    skip_reason: str = ""

    @property
    def status(self) -> str:
        if not self.available:
            return "skipped"
        if self.passed:
            return "passed"
        return "failed"


@dataclass
class ValidationReport:
    target_dir: str
    tiers: List[TierResult] = field(default_factory=list)
    total_duration_s: float = 0.0

    @property
    def overall_passed(self) -> bool:
        # Skipped tiers don't fail the report; only available+!passed do.
        return not any(
            t.available and not t.passed
            for t in self.tiers
        )

    @property
    def summary(self) -> dict:
        return {
            "target_dir": self.target_dir,
            "total_duration_s": round(self.total_duration_s, 2),
            "overall_passed": self.overall_passed,
            "tiers": [
                {
                    "tier": t.tier,
                    "name": t.name,
                    "status": t.status,
                    "files_checked": t.files_checked,
                    "failure_count": len(t.failures),
                    "skip_reason": t.skip_reason,
                }
                for t in self.tiers
            ],
        }


def is_terragrunt_available() -> bool:
    """True iff `terragrunt` is on PATH."""
    return shutil.which("terragrunt") is not None


def _count_hcl_files(target_dir: str) -> int:
    """Count .hcl + terragrunt.hcl files under target_dir (proxy for
    'files checked' count in Tier 1/2 reports — terragrunt doesn't
    easily expose how many files it actually processed)."""
    count = 0
    for _root, _dirs, files in os.walk(target_dir):
        for fname in files:
            if fname.endswith(".hcl"):
                count += 1
    return count


def validate_target(target_dir: str) -> ValidationReport:
    """Run all available tiers against the emitted target/ tree.

    Best-effort: each tier handles its own failures internally and
    records them on the TierResult. Never raises.
    """
    import time as _time

    started = _time.monotonic()
    report = ValidationReport(target_dir=os.path.abspath(target_dir))

    if not os.path.isdir(target_dir):
        # Synthesize a failed Tier 0 so the UI surfaces a clear error.
        report.tiers.append(TierResult(
            tier=0,
            name="HCL parses",
            available=True,
            passed=False,
            failures=[f"target directory does not exist: {target_dir}"],
        ))
        report.total_duration_s = round(_time.monotonic() - started, 2)
        return report

    report.tiers.append(_tier0_hcl_parses(target_dir))

    tg_available = is_terragrunt_available()
    report.tiers.append(_tier1_hcl_format(target_dir, tg_available))
    report.tiers.append(_tier2_hcl_validate(target_dir, tg_available))

    report.total_duration_s = round(_time.monotonic() - started, 2)
    return report


# -----------------------------------------------------------------
# Tier 0: HCL parses (native, always available)
# -----------------------------------------------------------------

def _tier0_hcl_parses(target_dir: str) -> TierResult:
    import time as _time

    started = _time.monotonic()
    failures: List[str] = []
    count = 0

    try:
        import hcl2  # type: ignore
    except ImportError:
        return TierResult(
            tier=0,
            name="HCL parses",
            available=False,
            passed=False,
            skip_reason="python-hcl2 not installed",
        )

    for root, _dirs, files in os.walk(target_dir):
        for fname in files:
            if not (fname.endswith(".hcl") or fname.endswith(".tf")):
                continue
            count += 1
            full = os.path.join(root, fname)
            try:
                with open(full, "r", encoding="utf-8") as fh:
                    hcl2.load(fh)
            except Exception as e:  # noqa: BLE001
                rel = os.path.relpath(full, target_dir).replace(os.sep, "/")
                failures.append(f"{rel}: {type(e).__name__}: {str(e)[:120]}")

    return TierResult(
        tier=0,
        name="HCL parses",
        available=True,
        passed=len(failures) == 0,
        duration_s=round(_time.monotonic() - started, 2),
        files_checked=count,
        failures=failures[:50],   # cap for UI sanity
    )


# -----------------------------------------------------------------
# Tier 1: terragrunt hcl format --check  (terragrunt 1.0+ syntax)
# -----------------------------------------------------------------

def _tier1_hcl_format(target_dir: str, tg_available: bool) -> TierResult:
    if not tg_available:
        return TierResult(
            tier=1,
            name="terragrunt hcl format --check",
            available=False,
            passed=False,
            skip_reason="terragrunt CLI not on PATH (install: https://terragrunt.gruntwork.io/docs/getting-started/install/)",
        )

    import time as _time
    started = _time.monotonic()
    try:
        proc = subprocess.run(
            ["terragrunt", "hcl", "format", "--check",
             "--working-dir", target_dir, "--no-color"],
            capture_output=True, text=True, timeout=60,
        )
        passed = proc.returncode == 0
        failures = []
        if not passed:
            for line in (proc.stdout + "\n" + proc.stderr).splitlines():
                line = line.strip()
                # Skip log-prefix noise; keep the meaningful "needs formatting" lines
                if line and len(failures) < 50:
                    if "ERROR" in line or "needs formatting" in line or "WARN" in line:
                        failures.append(line[:200])
    except subprocess.TimeoutExpired:
        return TierResult(
            tier=1,
            name="terragrunt hcl format --check",
            available=True,
            passed=False,
            duration_s=round(_time.monotonic() - started, 2),
            failures=["terragrunt hcl format timed out after 60s"],
        )

    return TierResult(
        tier=1,
        name="terragrunt hcl format --check",
        available=True,
        passed=passed,
        duration_s=round(_time.monotonic() - started, 2),
        files_checked=_count_hcl_files(target_dir),
        failures=failures,
    )


# -----------------------------------------------------------------
# Tier 2: terragrunt hcl validate  (terragrunt 1.0+ syntax)
# -----------------------------------------------------------------

def _tier2_hcl_validate(target_dir: str, tg_available: bool) -> TierResult:
    if not tg_available:
        return TierResult(
            tier=2,
            name="terragrunt hcl validate",
            available=False,
            passed=False,
            skip_reason="terragrunt CLI not on PATH",
        )

    import time as _time
    started = _time.monotonic()
    try:
        proc = subprocess.run(
            ["terragrunt", "hcl", "validate",
             "--working-dir", target_dir, "--no-color"],
            capture_output=True, text=True, timeout=120,
        )
        passed = proc.returncode == 0
        failures = []
        if not passed:
            for line in (proc.stdout + "\n" + proc.stderr).splitlines():
                line = line.strip()
                if line and len(failures) < 50:
                    if "Error" in line or "│" in line or "ERROR" in line:
                        failures.append(line[:300])
    except subprocess.TimeoutExpired:
        return TierResult(
            tier=2,
            name="terragrunt hcl validate",
            available=True,
            passed=False,
            duration_s=round(_time.monotonic() - started, 2),
            failures=["terragrunt hcl validate timed out after 120s"],
        )
    except FileNotFoundError:
        return TierResult(
            tier=2,
            name="terragrunt hcl validate",
            available=False,
            passed=False,
            duration_s=round(_time.monotonic() - started, 2),
            skip_reason="terragrunt subcommand `hcl validate` not present (requires terragrunt 1.0+)",
        )

    return TierResult(
        tier=2,
        name="terragrunt hcl validate",
        available=True,
        passed=passed,
        duration_s=round(_time.monotonic() - started, 2),
        files_checked=_count_hcl_files(target_dir),
        failures=failures,
    )


# -----------------------------------------------------------------
# Tier 3 was `terragrunt validate-inputs` — removed in terragrunt 1.0+.
# Replacement (`terragrunt run -- validate -inputs`) requires the
# module to be initialized + cloud creds, which the always-on tier
# can't satisfy. Dropped from the active tier set; will return as a
# Tier 4 (operator-triggered, AWS-creds required) in v2.
# -----------------------------------------------------------------
