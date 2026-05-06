"""Tiers 0–3 validation of an emitted target/ tree.

Each tier returns a ValidationTierResult; the orchestrator collects
them into a ValidationReport. UI renders this report in the
Validation tab.

Tier 0 is always run (uses python-hcl2 from ingest — already a
dependency). Tiers 1–3 require the `terragrunt` CLI on PATH; if
not present, those tiers report `available=False` with a one-line
install hint.
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
    report.tiers.append(_tier1_hclfmt(target_dir, tg_available))
    report.tiers.append(_tier2_hclvalidate(target_dir, tg_available))
    report.tiers.append(_tier3_validate_inputs(target_dir, tg_available))

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
# Tier 1: terragrunt hclfmt --check
# -----------------------------------------------------------------

def _tier1_hclfmt(target_dir: str, tg_available: bool) -> TierResult:
    if not tg_available:
        return TierResult(
            tier=1,
            name="terragrunt hclfmt --check",
            available=False,
            passed=False,
            skip_reason="terragrunt CLI not on PATH (install: https://terragrunt.gruntwork.io/docs/getting-started/install/)",
        )

    import time as _time
    started = _time.monotonic()
    try:
        proc = subprocess.run(
            ["terragrunt", "hclfmt", "--check", "--terragrunt-working-dir", target_dir],
            capture_output=True, text=True, timeout=60,
        )
        passed = proc.returncode == 0
        failures = []
        if not passed:
            for line in (proc.stdout + "\n" + proc.stderr).splitlines():
                line = line.strip()
                if line and len(failures) < 50:
                    failures.append(line[:200])
    except subprocess.TimeoutExpired:
        return TierResult(
            tier=1,
            name="terragrunt hclfmt --check",
            available=True,
            passed=False,
            duration_s=round(_time.monotonic() - started, 2),
            failures=["terragrunt hclfmt timed out after 60s"],
        )

    return TierResult(
        tier=1,
        name="terragrunt hclfmt --check",
        available=True,
        passed=passed,
        duration_s=round(_time.monotonic() - started, 2),
        failures=failures,
    )


# -----------------------------------------------------------------
# Tier 2: terragrunt hclvalidate
# -----------------------------------------------------------------

def _tier2_hclvalidate(target_dir: str, tg_available: bool) -> TierResult:
    if not tg_available:
        return TierResult(
            tier=2,
            name="terragrunt hclvalidate",
            available=False,
            passed=False,
            skip_reason="terragrunt CLI not on PATH",
        )

    import time as _time
    started = _time.monotonic()
    try:
        proc = subprocess.run(
            ["terragrunt", "hclvalidate", "--terragrunt-working-dir", target_dir],
            capture_output=True, text=True, timeout=120,
        )
        passed = proc.returncode == 0
        failures = []
        if not passed:
            for line in (proc.stdout + "\n" + proc.stderr).splitlines():
                line = line.strip()
                if line and len(failures) < 50:
                    failures.append(line[:300])
    except subprocess.TimeoutExpired:
        return TierResult(
            tier=2,
            name="terragrunt hclvalidate",
            available=True,
            passed=False,
            duration_s=round(_time.monotonic() - started, 2),
            failures=["terragrunt hclvalidate timed out after 120s"],
        )
    except FileNotFoundError:
        # `terragrunt hclvalidate` is a newer subcommand (added 0.55+).
        # Older versions don't have it. Report as available=True but
        # skipped for compatibility.
        return TierResult(
            tier=2,
            name="terragrunt hclvalidate",
            available=False,
            passed=False,
            duration_s=round(_time.monotonic() - started, 2),
            skip_reason="terragrunt subcommand `hclvalidate` not present (requires terragrunt 0.55+)",
        )

    return TierResult(
        tier=2,
        name="terragrunt hclvalidate",
        available=True,
        passed=passed,
        duration_s=round(_time.monotonic() - started, 2),
        failures=failures,
    )


# -----------------------------------------------------------------
# Tier 3: terragrunt validate-inputs --terragrunt-strict-validate
# -----------------------------------------------------------------

def _tier3_validate_inputs(target_dir: str, tg_available: bool) -> TierResult:
    """Run validate-inputs across every leaf with a non-empty inputs block.

    Caveat: this requires terragrunt to RESOLVE the module source so
    it can compare inputs against the module's variable declarations.
    Local relative-path modules (our default) work; remote git modules
    require network + auth.

    To stay fast and offline-capable for the demo, we sample the first
    N leaf stacks rather than running across all 1,050. Operator
    triggers full validation on demand.
    """
    if not tg_available:
        return TierResult(
            tier=3,
            name="terragrunt validate-inputs",
            available=False,
            passed=False,
            skip_reason="terragrunt CLI not on PATH",
        )

    import time as _time
    started = _time.monotonic()

    # Find leaf terragrunt.hcl files (skip the root + _common dir).
    candidates = _find_leaf_stacks(target_dir, max_count=20)
    if not candidates:
        return TierResult(
            tier=3,
            name="terragrunt validate-inputs",
            available=True,
            passed=True,
            duration_s=round(_time.monotonic() - started, 2),
            files_checked=0,
            skip_reason="no leaf stacks found",
        )

    failures: List[str] = []
    for leaf_dir in candidates:
        try:
            proc = subprocess.run(
                [
                    "terragrunt",
                    "validate-inputs",
                    "--terragrunt-strict-validate",
                    "--terragrunt-working-dir", leaf_dir,
                ],
                capture_output=True, text=True, timeout=30,
            )
            if proc.returncode != 0:
                rel = os.path.relpath(leaf_dir, target_dir).replace(os.sep, "/")
                first_err = ""
                for line in (proc.stderr + "\n" + proc.stdout).splitlines():
                    line = line.strip()
                    if line and "ERROR" in line.upper():
                        first_err = line[:200]
                        break
                if not first_err:
                    first_err = (proc.stderr.strip() or proc.stdout.strip() or "non-zero exit").splitlines()[0][:200]
                failures.append(f"{rel}: {first_err}")
        except subprocess.TimeoutExpired:
            rel = os.path.relpath(leaf_dir, target_dir).replace(os.sep, "/")
            failures.append(f"{rel}: timeout")
        except Exception as e:  # noqa: BLE001
            rel = os.path.relpath(leaf_dir, target_dir).replace(os.sep, "/")
            failures.append(f"{rel}: {type(e).__name__}: {str(e)[:160]}")

    return TierResult(
        tier=3,
        name="terragrunt validate-inputs (sampled, first 20 stacks)",
        available=True,
        passed=len(failures) == 0,
        duration_s=round(_time.monotonic() - started, 2),
        files_checked=len(candidates),
        failures=failures[:50],
    )


def _find_leaf_stacks(target_dir: str, max_count: int = 20) -> List[str]:
    """Find leaf stack directories — those with a terragrunt.hcl that
    isn't the root one (root is the immediate child of target_dir).

    We sample the first `max_count` for Tier 3 sanity check; running
    against all 1,050 would take 5–10 minutes which kills the demo flow.
    """
    out: List[str] = []
    abs_target = os.path.abspath(target_dir)
    for root, _dirs, files in os.walk(target_dir):
        if "terragrunt.hcl" not in files:
            continue
        if os.path.abspath(root) == abs_target:
            continue   # skip the root terragrunt.hcl
        if "_common" in root.split(os.sep) or "_envcommon" in root.split(os.sep):
            continue
        out.append(root)
        if len(out) >= max_count:
            break
    return out
