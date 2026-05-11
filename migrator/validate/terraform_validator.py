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


def validate_target(target_dir: str, *, skip_tier2: bool = False) -> ValidationReport:
    """Run all available tiers against a pure-Terraform target/ tree.

    Args:
        skip_tier2: if True, skip the slow `terraform init + validate`
            step. Tier 0/1 still run. Useful for fast preview / demo
            mode where the operator wants Discover + Plan + Generate
            output but doesn't need provider-schema verification.
    """
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

    if skip_tier2:
        report.tiers.append(TierResult(
            tier=2,
            name="terraform init + validate",
            available=False,    # render as ⚪ SKIPPED, not ✅ PASSED
            passed=False,
            skip_reason="opted out by operator (fast preview mode — uncheck 'Skip Tier 2' to enable)",
        ))
    else:
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
# Run once per detected root module (per env), IN PARALLEL across envs.
# `init -backend=false` downloads providers + module sources without
# touching cloud creds. `validate` checks provider schema +
# cross-resource references. Both calls per env are independent
# (separate cwd, separate subprocess, no shared state), so we run
# them concurrently across envs via a ThreadPoolExecutor. For a 3-env
# repo this is ~3x faster end-to-end than sequential.
# -----------------------------------------------------------------

# Cap on parallel terraform processes. Each one talks to the provider
# registry on first init, so too many in flight can saturate network /
# trip rate limits. 4 is a sweet spot for typical 3-5 env repos.
_TIER2_MAX_PARALLEL = 4

# Detect TF_PLUGIN_CACHE_DIR — if set, we DON'T wipe .terraform/
# after each run (the cache is the customer's investment in speed;
# wiping defeats it). With the cache the actual provider binaries
# live outside the working dir, so .terraform/ is just symlinks
# and the disk-footprint argument for wiping doesn't apply.
def _plugin_cache_active() -> bool:
    return bool(os.environ.get("TF_PLUGIN_CACHE_DIR", "").strip())


def _validate_one_root(root: str, target_dir: str) -> dict:
    """Run init + validate against a single root module directory.

    Returns a dict with keys:
        rel_root: str       — path relative to target_dir for failure messages
        passed:   bool      — True iff both init and validate returned 0
        failures: List[str] — one-line failure summaries (capped at 50)
    """
    rel_root = os.path.relpath(root, target_dir).replace(os.sep, "/")
    failures: List[str] = []
    passed = True

    # Pre-flight cleanup: wipe any stale .terraform/ from a previous run.
    # init can otherwise pick up cached provider binary mismatches.
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
        return {"rel_root": rel_root, "passed": False,
                "failures": [f"{rel_root}: init timed out after 180s"]}
    except OSError as e:
        return {"rel_root": rel_root, "passed": False,
                "failures": [f"{rel_root}: init failed to launch: {e}"]}

    if init_proc.returncode != 0:
        passed = False
        for line in (init_proc.stdout + "\n" + init_proc.stderr).splitlines():
            line = line.strip()
            if line and len(failures) < 50:
                if "Error" in line or "error" in line.lower() or "│" in line:
                    failures.append(f"{rel_root}: init: {line[:250]}")
        return {"rel_root": rel_root, "passed": passed, "failures": failures}

    # terraform validate
    try:
        val_proc = subprocess.run(
            ["terraform", "validate", "-no-color"],
            cwd=root,
            capture_output=True, text=True, timeout=60,
        )
    except subprocess.TimeoutExpired:
        return {"rel_root": rel_root, "passed": False,
                "failures": [f"{rel_root}: validate timed out after 60s"]}

    if val_proc.returncode != 0:
        passed = False
        for line in (val_proc.stdout + "\n" + val_proc.stderr).splitlines():
            line = line.strip()
            if line and len(failures) < 50:
                if "Error" in line or "│" in line:
                    failures.append(f"{rel_root}: validate: {line[:250]}")

    # Post-run cleanup of .terraform/ — keeps the output dir shippable
    # (no 100+MB of provider binaries to ZIP). Skipped when
    # TF_PLUGIN_CACHE_DIR is set, because in that case .terraform/
    # only contains symlinks to the cache (not the actual binaries),
    # and preserving them lets the next run skip provider download.
    if os.path.isdir(_terraform_cache) and not _plugin_cache_active():
        shutil.rmtree(_terraform_cache, ignore_errors=True)

    return {"rel_root": rel_root, "passed": passed, "failures": failures}


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
    overall_passed = True
    # files_checked = number of ROOT MODULES validated (one per env);
    # `terraform validate` pulls in every referenced module body
    # transitively, so a root-count is the meaningful unit. UI labels
    # this "Root modules" instead of "Files checked" for Tier 2.
    files_checked = len(roots)

    # Run all roots in parallel. ThreadPoolExecutor is appropriate
    # because each worker spends its time in subprocess.run() (blocked
    # on terraform CLI), which releases the GIL.
    max_workers = min(len(roots), _TIER2_MAX_PARALLEL)
    from concurrent.futures import ThreadPoolExecutor, as_completed
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = [pool.submit(_validate_one_root, r, target_dir) for r in roots]
        for fut in as_completed(futures):
            try:
                result = fut.result()
            except Exception as e:  # noqa: BLE001 — surface but don't crash
                overall_passed = False
                failures.append(f"validation worker crashed: {type(e).__name__}: {e}")
                continue
            if not result["passed"]:
                overall_passed = False
            # Cap aggregated failures at 50 for UI sanity.
            for f in result["failures"]:
                if len(failures) < 50:
                    failures.append(f)

    return TierResult(
        tier=2,
        name="terraform init + validate",
        available=True,
        passed=overall_passed,
        duration_s=round(_time.monotonic() - started, 2),
        files_checked=files_checked,
        failures=failures,
    )
