"""Tiers 0-2 validation for an emitted pure-Terraform target/ tree.

Companion to terragrunt_validator.py — same TierResult / ValidationReport
shape, different commands. UI Validation tab renders both via the
common shape.

  Tier 0: HCL parses           — native (python-hcl2; same as terragrunt)
  Tier 1: terraform fmt -check — format conformance (recursive over target/)
  Tier 2: terraform init -backend=false + terraform validate
          — provider schema check, run per env directory

Tier 4-6 deferred to v2 (need cloud creds for plan/apply).
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
from contextlib import contextmanager
from typing import List

from .terragrunt_validator import TierResult, ValidationReport

logger = logging.getLogger(__name__)


def is_terraform_available() -> bool:
    """True iff `terraform` is on PATH."""
    return shutil.which("terraform") is not None


def _count_tf_files(target_dir: str) -> int:
    """Count .tf files under target_dir."""
    count = 0
    for _root, _dirs, files in os.walk(target_dir):
        for fname in files:
            if fname.endswith(".tf"):
                count += 1
    return count


def _find_root_modules(target_dir: str) -> List[str]:
    """Find directories that are Terraform root modules (have providers.tf
    OR backend.tf — i.e. envs, not shared modules).

    Only descends into target_dir/environments/ if it exists; otherwise
    treats target_dir itself as the single root.
    """
    envs_dir = os.path.join(target_dir, "environments")
    if os.path.isdir(envs_dir):
        roots = []
        for entry in sorted(os.listdir(envs_dir)):
            sub = os.path.join(envs_dir, entry)
            if os.path.isdir(sub) and (
                os.path.isfile(os.path.join(sub, "providers.tf"))
                or os.path.isfile(os.path.join(sub, "backend.tf"))
                or os.path.isfile(os.path.join(sub, "main.tf"))
            ):
                roots.append(sub)
        if roots:
            return roots
    # Fallback: target_dir itself if it has a main.tf
    if os.path.isfile(os.path.join(target_dir, "main.tf")):
        return [target_dir]
    return []


def validate_target(target_dir: str) -> ValidationReport:
    """Run all available tiers against a pure-Terraform target/ tree."""
    import time as _time

    started = _time.monotonic()
    report = ValidationReport(target_dir=os.path.abspath(target_dir))

    if not os.path.isdir(target_dir):
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

    tf_available = is_terraform_available()
    report.tiers.append(_tier1_terraform_fmt(target_dir, tf_available))
    report.tiers.append(_tier2_terraform_validate(target_dir, tf_available))

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
        failures=failures[:50],
    )


# -----------------------------------------------------------------
# Tier 1: terraform fmt -check -recursive
# -----------------------------------------------------------------

def _tier1_terraform_fmt(target_dir: str, tf_available: bool) -> TierResult:
    if not tf_available:
        return TierResult(
            tier=1,
            name="terraform fmt -check",
            available=False,
            passed=False,
            skip_reason="terraform CLI not on PATH",
        )

    import time as _time
    started = _time.monotonic()
    try:
        proc = subprocess.run(
            ["terraform", "fmt", "-check", "-recursive", target_dir],
            capture_output=True, text=True, timeout=60,
        )
        passed = proc.returncode == 0
        failures: List[str] = []
        if not passed:
            # `terraform fmt -check` lists files that need formatting on stdout
            for line in proc.stdout.splitlines():
                line = line.strip()
                if line and len(failures) < 50:
                    failures.append(f"needs formatting: {line}")
            for line in proc.stderr.splitlines():
                line = line.strip()
                if line and len(failures) < 50:
                    failures.append(line[:200])
    except subprocess.TimeoutExpired:
        return TierResult(
            tier=1,
            name="terraform fmt -check",
            available=True,
            passed=False,
            duration_s=round(_time.monotonic() - started, 2),
            failures=["terraform fmt timed out after 60s"],
        )

    return TierResult(
        tier=1,
        name="terraform fmt -check",
        available=True,
        passed=passed,
        duration_s=round(_time.monotonic() - started, 2),
        files_checked=_count_tf_files(target_dir),
        failures=failures,
    )


# -----------------------------------------------------------------
# Tier 2: terraform init -backend=false  +  terraform validate
#
# Run once per detected root module (per env). `init -backend=false`
# downloads providers + module sources without touching cloud creds.
# `validate` checks provider schema + cross-resource references.
# -----------------------------------------------------------------

def _tier2_terraform_validate(target_dir: str, tf_available: bool) -> TierResult:
    if not tf_available:
        return TierResult(
            tier=2,
            name="terraform init + validate",
            available=False,
            passed=False,
            skip_reason="terraform CLI not on PATH",
        )

    import time as _time
    started = _time.monotonic()

    roots = _find_root_modules(target_dir)
    if not roots:
        return TierResult(
            tier=2,
            name="terraform init + validate",
            available=True,
            passed=False,
            duration_s=round(_time.monotonic() - started, 2),
            failures=["no Terraform root module found under target/"],
        )

    failures: List[str] = []
    files_checked = 0
    overall_passed = True

    for root in roots:
        rel_root = os.path.relpath(root, target_dir).replace(os.sep, "/")
        files_checked += sum(1 for f in os.listdir(root) if f.endswith(".tf"))

        # Clean up .terraform/ from a previous validation run; otherwise
        # init can hit cached provider binary mismatches and we end up
        # with stale artifacts in the output dir.
        _terraform_cache = os.path.join(root, ".terraform")
        if os.path.isdir(_terraform_cache):
            shutil.rmtree(_terraform_cache, ignore_errors=True)

        # init -backend=false  (no remote state contact)
        try:
            init_proc = subprocess.run(
                ["terraform", "init", "-backend=false", "-input=false", "-no-color"],
                cwd=root,
                capture_output=True, text=True, timeout=180,
            )
        except subprocess.TimeoutExpired:
            failures.append(f"{rel_root}: init timed out after 180s")
            overall_passed = False
            continue
        except OSError as e:
            failures.append(f"{rel_root}: init failed to launch: {e}")
            overall_passed = False
            continue

        if init_proc.returncode != 0:
            overall_passed = False
            for line in (init_proc.stdout + "\n" + init_proc.stderr).splitlines():
                line = line.strip()
                if line and len(failures) < 50:
                    if "Error" in line or "error" in line.lower() or "│" in line:
                        failures.append(f"{rel_root}: init: {line[:250]}")
            continue

        # terraform validate
        try:
            val_proc = subprocess.run(
                ["terraform", "validate", "-no-color"],
                cwd=root,
                capture_output=True, text=True, timeout=60,
            )
        except subprocess.TimeoutExpired:
            failures.append(f"{rel_root}: validate timed out after 60s")
            overall_passed = False
            continue

        if val_proc.returncode != 0:
            overall_passed = False
            for line in (val_proc.stdout + "\n" + val_proc.stderr).splitlines():
                line = line.strip()
                if line and len(failures) < 50:
                    if "Error" in line or "│" in line:
                        failures.append(f"{rel_root}: validate: {line[:250]}")

        # Clean up the .terraform/ provider cache — keeps the output
        # directory shippable (no 100+MB of provider binaries to ZIP).
        # The .terraform.lock.hcl stays (pins provider versions).
        if os.path.isdir(_terraform_cache):
            shutil.rmtree(_terraform_cache, ignore_errors=True)

    return TierResult(
        tier=2,
        name="terraform init + validate",
        available=True,
        passed=overall_passed,
        duration_s=round(_time.monotonic() - started, 2),
        files_checked=files_checked,
        failures=failures,
    )
