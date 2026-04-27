# detector/remediator.py
"""
Interactive drift remediation.

After the diff_engine produces a report, this module walks the user through
each drifted resource and offers per-resource actions.

For *modified* drift (state and cloud differ on a tracked field):
    [R]estore  — push recorded state back to cloud
                 (terraform plan -target=ADDR  ->  apply -target=ADDR -auto-approve)
    [A]ccept   — pull cloud changes into state, leaving cloud + HCL untouched
                 (terraform plan -refresh-only -target=ADDR  ->  apply -refresh-only ...)
    [S]kip / [Q]uit

For *missing* drift (cloud resource was deleted out-of-band):
    [R]ecreate — `terraform apply -target=ADDR` to put the resource back
    [D]rop     — `terraform state rm ADDR` so terraform stops tracking it
    [S]kip / [Q]uit

Both [R]ecreate and [D]rop require typed-name confirmation because the cost
of an accidental keystroke is large (a full reprovision, or losing a state
entry that points at a real cloud resource).

Safety rails baked into every state-mutating action:

  - **Pre-flight backup.** Before any apply / state-rm we copy
    `terraform.tfstate` to a timestamped sibling. Terraform's own
    `terraform.tfstate.backup` is overwritten on the next apply, so two
    back-to-back remediations would lose the original; ours survives.
  - **Post-apply re-verify.** After apply we re-read state, re-fetch the
    cloud snapshot, and re-diff just the touched resource. This catches
    the false-success case where `apply` returns 0 but cloud still differs
    (eventual consistency, schema mismatch, fields TF doesn't track).
  - **No-op detection on plan.** `-detailed-exitcode` lets us distinguish
    "plan errored" (1) from "no changes to make" (0) from "ready to apply"
    (2). Without this, an unmanaged-field drift would yield apply-as-no-op
    and a falsely cheerful "[OK] Restored" report.

Design choices worth flagging:

  - We delegate execution to `terraform` rather than computing reverse gcloud
    commands ourselves. Slower, and -target is officially "for exceptional
    circumstances" per TF docs, but it gives us resource-type-agnostic
    behaviour for free.
  - We auto-prompt only on a tty (sys.stdin.isatty()). In CI / piped runs
    the prompt is silently skipped so the pipeline doesn't hang on input().
  - Step 3 (deferred) will add Rich side-by-side panels and a [V]iew action.
"""

import os
import shutil
import subprocess
import sys
import threading
import time
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import cloud_snapshot, config, state_reader
from .diff_engine import ResourceDrift, diff_resource

# P4-1 (CC-2 detector half): per-operation timeout budgets for terraform
# subprocess invocations. Phase 0 audit flagged that detector shells out to
# `terraform plan/apply/init/import/state` with no timeout -- a slow
# upstream wedges the request indefinitely and Cloud Run's 60-min request
# timeout is the only backstop.
#
# Budgets per the punchlist CC-2 spec. Conservative -- a slow-but-real run
# completes; only genuine hangs get killed:
_TERRAFORM_TIMEOUTS: Dict[str, int] = {
    "init":    600,   # downloads providers; slow first run, fast on rerun
    "plan":    300,   # per -target invocation
    "apply":   600,   # per -target apply (largest changes are still <10min)
    "refresh": 300,   # refresh-only apply (state sync, no resource mutation)
    "import":  120,   # per resource (single API roundtrip + state write)
    "state":    60,   # state subcommands (rm, mv, list) -- in-memory ops
}
# Default for any subcommand not in the map. Same as plan -- conservative
# enough to cover most ad-hoc operations without locking up indefinitely.
_TERRAFORM_DEFAULT_TIMEOUT = 300


# --- Summary record -------------------------------------------------------

@dataclass
class RemediationSummary:
    restored: List[str] = field(default_factory=list)
    accepted: List[str] = field(default_factory=list)
    skipped: List[str] = field(default_factory=list)
    failed: List[Tuple[str, str]] = field(default_factory=list)  # (addr, op)


@dataclass
class RemediationResult:
    """Structured outcome of a single `remediate_one()` call.

    The CLI summary path uses bool returns from the action helpers; the
    programmatic API needs a richer object so a UI can render a status
    badge without parsing stdout. `success` is the coarse signal; `status`
    distinguishes "ok" from "failed" / "invalid_action" / "exception" so
    callers can treat user-declines or no-op plans differently from real
    errors if they want to.
    """
    tf_address: str
    action: str
    success: bool
    status: str = "ok"
    message: str = ""


# --- Confirmation policy (headless-friendly) ----------------------------
#
# Why this exists
# ---------------
# Every action helper used to call `input()` directly. That's fine for the
# CLI path but deadlocks any caller without a stdin — Streamlit worker
# threads, integration tests, the upcoming FastAPI endpoint. Pulling the
# user-confirmation surface behind a policy object means the action
# helpers don't change shape between CLI and headless modes; only the
# policy passed in changes.
#
# Two methods cover everything the helpers do today:
#   - yes_no(prompt, default): single-keystroke Y/N gates ("Proceed with
#     apply? [y/N]"). Returns "Y" or "N".
#   - typed(expected, prompt):  type-back-the-address gates used before
#     destructive ops (recreate, drop). Returns True if the user typed it
#     correctly, False otherwise.
#
# Multi-choice action pickers (R/A/S/Q) live only in run_remediation()
# and stay CLI-only — the programmatic API picks the action upstream
# and dispatches directly to a single handler, so there's nothing to
# generalise.

class ConfirmationPolicy:
    """Abstract base — subclass and override both methods."""
    def yes_no(self, prompt: str, *, default: str = "N") -> str:
        raise NotImplementedError
    def typed(self, expected: str, prompt: str) -> bool:
        raise NotImplementedError


class InteractivePolicy(ConfirmationPolicy):
    """The historical CLI path — block on stdin until the user answers.

    Same behavior as the pre-refactor inline `input()` calls: EOF on
    stdin returns the default (or False, for typed). A typed-confirm
    mismatch prints an "aborted" notice so the user sees why nothing
    happened.
    """
    def yes_no(self, prompt: str, *, default: str = "N") -> str:
        return _prompt(prompt, valid={"Y", "N"}, default=default)

    def typed(self, expected: str, prompt: str) -> bool:
        return _typed_confirm(expected, prompt)


class AutoConfirmPolicy(ConfirmationPolicy):
    """Programmatic confirmation — never blocks, returns the configured answer.

    Used by `remediate_one(..., auto_confirm=True)` and by the Streamlit
    UI worker thread. The contract: the human has already clicked the
    Restore/Accept/Drop button upstream, so by the time we reach this
    layer the confirmation has *already happened* — re-prompting would
    just deadlock. `typed()` returns True for the same reason: typed
    confirms are a CLI affordance against fat-finger keystrokes; a UI
    button click is its own (better) confirmation.
    """
    def __init__(self, *, answer: str = "Y"):
        self._answer = answer.upper()

    def yes_no(self, prompt: str, *, default: str = "N") -> str:
        return self._answer

    def typed(self, expected: str, prompt: str) -> bool:
        return True


# --- Small I/O helpers ---------------------------------------------------

def _is_interactive() -> bool:
    """True iff stdin is a real terminal. False in CI, piped runs, etc."""
    try:
        return sys.stdin.isatty()
    except (AttributeError, ValueError):
        return False


def _prompt(text: str, valid: set, default: Optional[str] = None) -> str:
    """
    Read one normalized keystroke from the user, validate against a set.

    `valid` is a set of single-letter uppercase choices (e.g. {"Y","N"}).
    If `default` is set, an empty answer maps to it.
    """
    valid_upper = {v.upper() for v in valid}
    while True:
        try:
            raw = input(text).strip().upper()
        except EOFError:
            # Stdin closed (test harness, redirected input). Fall back to
            # default if we have one; otherwise treat as quit.
            if default is not None:
                return default.upper()
            return "Q"
        if not raw and default is not None:
            return default.upper()
        if raw in valid_upper:
            return raw
        print(f"  Please choose one of: {', '.join(sorted(valid_upper))}")


# Subcommands that can prompt for un-defaulted variables and therefore need
# `-input=false` to fail fast instead of hanging. `terraform state rm` (and
# similar state subcommands) DON'T accept this flag and will reject it with
# "Error: Unsupported argument" — so we whitelist rather than blanket-inject.
_INPUT_FALSE_COMMANDS = frozenset({"plan", "apply", "refresh", "destroy", "import"})


def _start_kill_watchdog(proc: "subprocess.Popen[Any]",
                         timeout_s: int) -> threading.Thread:
    """Start a daemon thread that kills `proc` after `timeout_s` seconds.

    Cross-platform alternative to subprocess.run(timeout=) -- we can't
    use that because we want to STREAM stdout (the operator watches plan
    output flow in real time during a demo). Streaming requires Popen +
    iterating stdout, which doesn't accept a `timeout=` arg directly.

    The watchdog approach is intentionally simple: sleep for the budget,
    then check if the process is still running, kill if so. Race-free
    enough for our use case -- if the process exits a hair before the
    watchdog wakes, ``proc.poll()`` returns the real exit code and we
    skip the kill. If it exits during the kill, both calls are
    idempotent on POSIX and Windows.

    Daemon thread = no need to join; if the host process exits while
    the watchdog is sleeping, the thread dies too (no leaked thread).
    """
    def _watchdog() -> None:
        time.sleep(timeout_s)
        if proc.poll() is None:
            proc.kill()

    t = threading.Thread(target=_watchdog, daemon=True,
                         name="tf_kill_watchdog")
    t.start()
    return t


def _run_terraform(args: List[str], *, cwd: Optional[str] = None) -> int:
    """
    Stream a terraform invocation to stdout, return its exit code.

    We stream (Popen + iterate stdout) rather than capture (run + print)
    so the user sees plan output flow in real time during a demo. Slower
    feedback breaks the closed-loop feel.

    `cwd` is the per-project workdir. CLI callers don't need to pass it
    (the CLI chdir's once at entry). Programmatic callers (Streamlit /
    FastAPI worker threads) MUST pass it explicitly so two concurrent
    requests for two different projects don't fight over process cwd.

    Three non-obvious behaviours baked in:
      - `-input=false` is forced for plan/apply/refresh-family commands.
        Terraform parses ALL `.tf` files in the workspace before honouring
        -target, and if any declare a variable with no default (very common
        in translator output that lives in the same directory) it goes
        interactive and HANGS the remediator on a prompt. -input=false makes
        that fail fast with a clear error. We do NOT inject this for `state`
        subcommands, which reject unknown flags after the subcommand name.
      - `terraform` not on PATH is reported clearly rather than crashing.
      - **P4-1 timeout enforcement.** Per-operation budget from
        ``_TERRAFORM_TIMEOUTS`` enforced via a daemon watchdog thread (we
        can't use subprocess.run(timeout=) because we stream). On timeout
        the process is killed, exit code 124 is returned (GNU `timeout`
        convention), and a [TIMEOUT] line is printed for the operator.
        Phase 5 will lift this through the API layer as a typed
        ``UpstreamTimeout`` exception; today, callers see a non-zero
        exit code and the action is reported as failed -- same surface
        as any other terraform failure.
    """
    if args and args[0] in _INPUT_FALSE_COMMANDS:
        full_args = [args[0], "-input=false"] + list(args[1:])
    else:
        full_args = list(args)
    pretty = "terraform " + " ".join(full_args)
    print(f"  -> Running: {pretty}")

    # Resolve terraform binary via the common resolver. This used to be a
    # bare "terraform" + FileNotFoundError catch — same end result, but
    # now the error message points at the resolver's install hint instead
    # of just "not found on PATH" (which was misleading on machines where
    # the binary existed at the Windows default but wasn't on PATH).
    try:
        from common.terraform_path import resolve_terraform_path
        terraform_bin = resolve_terraform_path()
    except RuntimeError as e:
        print(f"  [FAIL] {e}")
        return 127

    try:
        proc = subprocess.Popen(
            [terraform_bin] + full_args,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            cwd=cwd,
            # Force UTF-8 decoding of terraform's stdout. Without this, Python
            # falls back to the system locale (cp1252 on Windows), which
            # mangles terraform's Unicode box-drawing chars (╷│╵) into mojibake.
            encoding="utf-8",
            errors="replace",
        )
    except FileNotFoundError:
        # Defensive: shouldn't happen post-resolver, but handle gracefully
        # if the resolved path becomes invalid between resolution and exec.
        print(f"  [FAIL] `{terraform_bin}` no longer exists. Action skipped.")
        return 127

    # P4-1: arm the timeout watchdog. Subcommand at full_args[0] -- after
    # any -input=false injection, this is still the subcommand name (e.g.
    # "plan", "init", "apply"). state subcommands ("state rm") are also
    # captured because we look at full_args[0] only.
    op = full_args[0] if full_args else "unknown"
    timeout_s = _TERRAFORM_TIMEOUTS.get(op, _TERRAFORM_DEFAULT_TIMEOUT)
    started = time.monotonic()
    _start_kill_watchdog(proc, timeout_s)

    assert proc.stdout is not None  # for type-checkers; Popen with PIPE guarantees this
    for line in proc.stdout:
        print(f"    {line.rstrip()}")
    proc.wait()
    elapsed = time.monotonic() - started

    # P4-1: detect watchdog kill. The kill races with normal exit, so we
    # use elapsed-time as the discriminator: if we ran for at least the
    # budget, the watchdog is the most likely cause. Belt-and-braces
    # check on exit code as well -- POSIX kill is -9, Windows kill is
    # typically 1 but varies. Elapsed-time is the more reliable signal.
    if elapsed >= timeout_s - 1 and proc.returncode != 0:
        print(f"  [TIMEOUT] terraform {op} exceeded {timeout_s}s budget "
              f"(ran {elapsed:.1f}s before watchdog killed). "
              f"Action failed.")
        return 124  # GNU timeout convention

    return proc.returncode


# --- Safety rails (state backup, post-apply re-verification) ------------

def _state_path(workdir: Optional[str] = None) -> str:
    """Resolve the active terraform.tfstate path.

    Per-project workdir refactor: state lives in the per-project workdir,
    NOT at the repo root any more (the repo root no longer has a
    terraform.tfstate after migrate_workdir.py ran).

    P4-1 (CC-2 detector hygiene): the previous ``workdir or os.getcwd()``
    fallback was the exact silent-cwd-fallback pattern that caused the
    per-project workdir refactor in the first place. A buggy programmatic
    caller that forgot to pass ``workdir`` would silently use the
    process cwd -- which on Cloud Run is the container root, not the
    requesting tenant's workdir, leading to wrong-tenant state reads.
    Now: a missing ``workdir`` raises ``PreflightError`` so the bug
    surfaces at the boundary instead of producing wrong-but-plausible
    results downstream.

    The CLI (detector/run.py) ALREADY passes ``workdir=workdir`` through
    every call site that reaches here -- verified across run.py L162 ->
    run_remediation -> action handlers -> _state_path. The cwd fallback
    was dead code; this commit removes it.

    Args:
        workdir: per-project workdir absolute path. Required.

    Returns:
        Absolute path to ``<workdir>/<STATE_FILE_NAME>``.

    Raises:
        PreflightError: workdir is None or empty. The caller is buggy
        and should plumb the workdir through explicitly.
    """
    if not workdir:
        # Lazy import to avoid pulling common.errors during module load
        # for callers that never need this branch (most CLI paths).
        from common.errors import PreflightError
        raise PreflightError(
            "_state_path() called without a workdir; refusing to fall "
            "back to process cwd (would risk wrong-tenant state reads "
            "under concurrency).",
            stage="resolve_workdir",
            reason="missing_workdir_arg",
        )
    return os.path.join(workdir, config.STATE_FILE_NAME)


def _backup_state(state_path: str) -> Optional[str]:
    """
    Snapshot terraform.tfstate to a timestamped sibling before any mutating
    apply. Returns the backup path on success, None on failure.

    Why our own backup, when terraform writes `terraform.tfstate.backup` for
    free? Because terraform overwrites that file on every apply — two
    back-to-back remediations would clobber the original pre-remediation
    state and leave the user nothing to roll back to. Ours is timestamped
    and untouched by terraform, so it survives subsequent applies and is
    easy to point a `cp` rollback at.
    """
    if not os.path.isfile(state_path):
        print(f"  [WARN]  Cannot back up state — file not found at {state_path}.")
        return None

    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_path = f"{state_path}.backup-{ts}"
    try:
        shutil.copy2(state_path, backup_path)
    except (OSError, IOError) as e:
        print(f"  [WARN]  State backup failed: {e}")
        return None
    print(f"  [STATE] State backed up -> {os.path.basename(backup_path)}")
    return backup_path


def _reverify(tf_address: str, state_path: str) -> Optional[ResourceDrift]:
    """
    Re-read state, re-fetch cloud, re-diff the single named resource.

    Returns the fresh ResourceDrift, or None if we couldn't even run the
    verification (e.g., the resource is no longer in state — expected after
    a `terraform state rm`).

    Reverify failure is non-fatal: the caller's apply may have succeeded
    regardless. The point of this function is to *report honestly* —
    distinguish "[OK] Restored AND verified in sync" from "[OK] apply ran clean
    BUT cloud still differs". Without it, our previous false-success bug
    (apply-as-no-op on unmanaged fields) silently shipped wrong reports.
    """
    resources = state_reader.read_state(state_path)
    target = next((r for r in resources if r.tf_address == tf_address), None)
    if target is None:
        # Expected after a successful `state rm`. Not an error.
        print(f"  [INFO]  {tf_address} is no longer present in state — nothing to re-diff.")
        return None

    snapshots = cloud_snapshot.fetch_snapshots([target])
    return diff_resource(
        tf_address=target.tf_address,
        tf_type=target.tf_type,
        state_attrs=target.attributes,
        cloud_json=snapshots.get(target.tf_address),
    )


def _print_reverify_result(drift: Optional[ResourceDrift]) -> None:
    """Render a one-line verdict on the post-apply re-diff."""
    if drift is None:
        return
    if drift.error:
        print(f"  [WARN]  Re-verify: {drift.error}")
        return
    if not drift.has_drift:
        print(f"  [OK] Re-verify: {drift.tf_address} is in sync with cloud.")
        return
    n = len(drift.items)
    print(f"  [WARN]  Re-verify: drift remains on {drift.tf_address} ({n} item(s)):")
    glyph = {"added": "+", "removed": "-", "changed": "~"}
    for item in drift.items:
        print(f"       {glyph.get(item.op, '?')} {item.path}")


def _typed_confirm(expected: str, prompt_text: str) -> bool:
    """
    Require the user to type back an exact string. Used as a second-level
    confirmation for actions where a stray Y could do real damage —
    Recreate (provisions a new cloud resource) and Drop (removes a real
    cloud-backed entry from state).
    """
    try:
        raw = input(prompt_text).strip()
    except EOFError:
        return False
    if raw == expected:
        return True
    print(f"  [SKIP]  Confirmation did not match (expected '{expected}'). Aborted.")
    return False


# --- Pre-apply policy gate (TODO #14, Brainboard parity) ----------------
#
# Why this exists
# ---------------
# Brainboard et al. surface OPA/Conftest violations alongside the apply
# diff so operators can't accidentally apply a change that would fail an
# audit. We already evaluate policies in the detector pass for reporting;
# this gate plumbs the same evaluation into the remediator so a Restore
# or Recreate that would land non-compliant state in the cloud is
# blocked (or at least loudly flagged) before any apply happens.
#
# Where it fires
# --------------
# Only `_restore` and `_recreate` get the gate. Rationale:
#   - Restore   pushes state -> cloud, so the post-apply cloud takes the
#               state's view of the resource. If state's view violates
#               policy, restore would introduce / preserve the violation.
#   - Recreate  provisions from HCL after an out-of-band delete. If the
#               HCL has policy-violating defaults, recreate would put a
#               non-compliant resource back into the cloud.
#   - Accept    pulls cloud -> state without changing cloud, so it cannot
#               INTRODUCE a new cloud-side violation.
#   - Drop      only mutates terraform.tfstate; cloud is already gone.
#
# Threshold
# ---------
# Default-blocks on HIGH only. MED/LOW print as warnings but don't gate.
# Mirrors the standalone CLI's `FAIL_AT_SEVERITY = "HIGH"` default in
# policy/config.py — operators can override per-call.
#
# Failure mode
# ------------
# Fail-OPEN: policy module missing, conftest missing, snapshot missing,
# or any internal error all evaluate to "no violations" and let the
# apply proceed. We never want a missing dep in the policy layer to
# block legitimate drift remediation. The detector's own classify_drift
# follows the same convention.

# Imported lazily inside the helper so importing remediator.py doesn't
# pull in conftest / policy machinery on systems that don't have it.
def _policy_check_for(tf_address: str,
                      cloud_snap: Optional[Dict[str, Any]] = None,
                      *,
                      workdir: Optional[str] = None):
    """Build a policy-evaluation closure for a single resource.

    Returned closure takes no arguments and yields a `PolicyImpact`.
    Closure form keeps the gate site (`_restore` / `_recreate`) free of
    snapshot-fetching logic and lets tests inject pre-built impacts.

    `cloud_snap` is the GCP describe-JSON snapshot. When None, the
    closure fetches it on demand from the live cloud via state lookup.
    Pre-supplying avoids a redundant cloud round-trip when the caller
    (typically the detector) already has the snapshot in hand.
    """
    def _check():
        try:
            from policy import integration as _policy
        except ImportError:
            return None  # policy module absent — fail-open
        snap = cloud_snap
        if snap is None:
            snap = _fetch_cloud_snapshot(tf_address, workdir=workdir)
        # tf_type is the first dotted component of the address. Resources
        # imported by the importer always have well-formed addresses.
        # P4-1: tightened from broad ``except Exception`` to ``IndexError``
        # (the only realistic failure -- empty string after split returns
        # [""] not raises, but [0] on [] would IndexError). AttributeError
        # if tf_address is None would surface as a real bug, not be
        # swallowed.
        try:
            tf_type = tf_address.split(".", 1)[0]
        except IndexError:
            return None
        try:
            return _policy.classify_drift(tf_address, tf_type, snap)
        except Exception as e:
            print(f"  [WARN] Policy gate evaluation failed for {tf_address}: {e}")
            return None
    return _check


def _fetch_cloud_snapshot(tf_address: str, *, workdir: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Fetch the live cloud snapshot for one resource address.

    Mirrors `_reverify` minus the diff step: read state, find the named
    resource, fetch its cloud snapshot. Returns None on any failure
    (state missing, address not in state, snapshot fetch errored) so
    the policy gate falls open.
    """
    try:
        state_path = _state_path(workdir=workdir)
        resources = state_reader.read_state(state_path)
        target = next((r for r in resources if r.tf_address == tf_address), None)
        if target is None:
            return None
        snapshots = cloud_snapshot.fetch_snapshots([target])
        return snapshots.get(target.tf_address)
    except Exception as e:
        print(f"  [WARN] Snapshot fetch failed for {tf_address}: {e}")
        return None


def _run_policy_gate(tf_address: str,
                     policy_check,
                     confirmation: ConfirmationPolicy,
                     *,
                     block_at: str = "HIGH") -> bool:
    """Evaluate policy and gate the apply on the result.

    Returns True if the apply should proceed (no blocking violations OR
    user/policy explicitly approved the override), False if blocked.

    Args:
        tf_address:   Address being remediated (for messages).
        policy_check: Closure returning a PolicyImpact, or None to skip.
        confirmation: Policy decides how to answer the override prompt.
        block_at:     Severity at or above which the gate blocks pending
                      explicit approval. "HIGH" (default) matches the
                      standalone CLI's FAIL_AT_SEVERITY.
    """
    if policy_check is None:
        return True
    impact = policy_check()
    if impact is None or not impact.is_violating:
        return True

    # Render every violation so the user (or audit log) sees the full picture.
    high = impact.high_count
    med = impact.med_count
    low = impact.low_count
    print(f"\n  [POLICY-GATE] Resource {tf_address} violates policy:")
    print(f"      HIGH={high}  MED={med}  LOW={low}")
    for v in impact.violations[:10]:  # cap at 10 so we don't drown the operator
        print(f"      - [{v.severity}][{v.rule_id}] {v.message}")
    if len(impact.violations) > 10:
        print(f"      - ... and {len(impact.violations) - 10} more")

    # Block decision: only count violations at or above the configured
    # threshold. MED/LOW alone print as warnings and let the apply proceed.
    blocking_count = {
        "HIGH": high,
        "MED":  high + med,
        "LOW":  high + med + low,
    }.get(block_at.upper(), high)
    if blocking_count == 0:
        print(f"  [POLICY-GATE] No {block_at}+ violations; apply proceeds.")
        return True

    answer = confirmation.yes_no(
        f"\n  [POLICY-GATE] Proceed despite {blocking_count} blocking "
        f"violation(s)? [y/N]: ",
        default="N",
    )
    if answer != "Y":
        print(f"  [POLICY-GATE] Apply blocked.")
        return False
    print(f"  [POLICY-GATE] Override accepted; proceeding with apply.")
    return True


# --- Per-resource action handlers ---------------------------------------

def _restore(tf_address: str,
             *,
             confirmation: ConfirmationPolicy,
             policy_check: Optional[Callable[[], Any]] = None,
             workdir: Optional[str] = None) -> bool:
    """
    Cloud <- state. Plan -> policy-gate -> backup -> apply -> re-verify.

    Returns True iff apply succeeded. Any failure (plan failure, no-op
    detected, user declines, policy block, backup failure, apply failure)
    returns False so the caller can record it in the summary. Residual
    drift after a successful apply is reported as success-with-warning,
    not failure — the apply did do its job; the gap is honest reporting.

    `policy_check`: closure returning a `PolicyImpact` (typically built by
    `_policy_check_for(...)`). When supplied, the gate fires *after* we
    confirm there's a real change to apply (plan exit 2) but *before* we
    bother the user with the apply confirmation prompt — a blocked apply
    is never offered. Defaults to None (gate skipped) so legacy callers
    keep working unchanged.

    Important semantic note: terraform can only restore drift on fields
    declared in HCL. If a drifted field is not in HCL (e.g., bucket has
    `labels` set out-of-band but HCL never mentions `labels`), terraform
    plan reports "0 to change" because it has no opinion on the field.
    Apply would then succeed-as-no-op, and we'd falsely report restore.
    We use `-detailed-exitcode` to catch that case loudly:
        exit 0  -> no changes planned (Restore is a no-op for this drift)
        exit 1  -> plan errored
        exit 2  -> changes planned (proceed to apply)
    """
    print("\n  Restore action: push recorded state back to cloud.")
    print("  Step 1/3 — generating plan...")
    rc = _run_terraform(["plan", f"-target={tf_address}", "-detailed-exitcode"], cwd=workdir)
    if rc == 1:
        print(f"  [FAIL] `terraform plan` errored (exit 1). Apply not attempted.")
        return False
    if rc == 0:
        # No-change plan — Restore via terraform is impossible for this drift.
        print()
        print("  [WARN]  terraform plan reports NO changes to make.")
        print("      The drifted field is not declared in your HCL, so terraform")
        print("      treats it as unmanaged and won't push any value to cloud.")
        print()
        print("      Three ways forward (pick the one matching your intent):")
        print("        1. Update HCL to declare the field with the desired value,")
        print("           then re-run Restore.")
        print("        2. Choose [A]ccept instead — pulls the cloud value into")
        print("           state. (HCL still won't reflect it; needs separate edit.)")
        print("        3. Revert directly via cloud SDK, e.g.:")
        print(f"             gcloud storage buckets update gs://<name> --remove-labels=<key>")
        print("             gcloud compute instances remove-labels <name> --labels=<key> ...")
        return False
    if rc != 2:
        print(f"  [FAIL] `terraform plan` returned unexpected exit code {rc}. Apply not attempted.")
        return False

    # Pre-apply policy gate (TODO #14). Brainboard parity: block a HIGH+
    # violation from sneaking through under the cover of a drift fix.
    # Fail-OPEN; see `_run_policy_gate` for thresholds and rationale.
    if not _run_policy_gate(tf_address, policy_check, confirmation):
        return False

    answer = confirmation.yes_no(
        "\n  Proceed with apply? [y/N]: ",
        default="N",
    )
    if answer != "Y":
        print("  [SKIP]  Apply skipped.")
        return False

    state_path = _state_path(workdir=workdir)
    print("  Step 2/3 — backing up state and applying...")
    if _backup_state(state_path) is None:
        print("  [FAIL] Refusing to apply without a state backup.")
        return False
    rc = _run_terraform(["apply", f"-target={tf_address}", "-auto-approve"], cwd=workdir)
    if rc != 0:
        print(f"  [FAIL] `terraform apply` exited {rc}.")
        return False

    print("  Step 3/3 — re-verifying against cloud...")
    drift = _reverify(tf_address, state_path)
    _print_reverify_result(drift)
    if drift is not None and drift.has_drift:
        # Apply ran clean but cloud still differs: don't lie about full closure.
        print(f"  [WARN]  Restored {tf_address}, but cloud still differs from state.")
        return True
    print(f"  [OK] Restored {tf_address} (verified in sync).")
    return True


def _accept(tf_address: str, *, confirmation: ConfirmationPolicy, workdir: Optional[str] = None) -> bool:
    """
    State <- cloud. Refresh-only plan -> backup -> refresh-only apply -> re-verify.

    Note: this updates terraform.tfstate to match the live cloud, but it
    does NOT update the .tf HCL files. Closing that loop fully needs an
    HCL regeneration pass — future cross-module integration with the importer.

    Same `-detailed-exitcode` no-op pattern as _restore: if cloud already
    matches state's view of it, refresh-only is a no-op and we say so loudly
    rather than silently reporting success on a non-action.
    """
    print("\n  Accept action: pull cloud changes into state.")
    print("  (HCL is NOT updated; expect a follow-up `terraform plan` diff against config.)")
    print("  Step 1/3 — generating refresh-only plan...")
    rc = _run_terraform([
        "plan", "-refresh-only", f"-target={tf_address}", "-detailed-exitcode",
    ], cwd=workdir)
    if rc == 1:
        print(f"  [FAIL] `terraform plan -refresh-only` errored (exit 1). Apply not attempted.")
        return False
    if rc == 0:
        print()
        print("  [WARN]  terraform plan -refresh-only reports NO changes to make.")
        print("      Cloud already matches what terraform reads back into state.")
        print("      The drift the detector reports may live in a field terraform")
        print("      doesn't track in its schema, or may be a detector false positive.")
        return False
    if rc != 2:
        print(f"  [FAIL] `terraform plan -refresh-only` returned unexpected exit code {rc}.")
        return False

    answer = confirmation.yes_no(
        "\n  Proceed with refresh-only apply? [y/N]: ",
        default="N",
    )
    if answer != "Y":
        print("  [SKIP]  Apply skipped.")
        return False

    state_path = _state_path(workdir=workdir)
    print("  Step 2/3 — backing up state and applying refresh-only...")
    if _backup_state(state_path) is None:
        print("  [FAIL] Refusing to apply without a state backup.")
        return False
    rc = _run_terraform([
        "apply", "-refresh-only", f"-target={tf_address}", "-auto-approve",
    ], cwd=workdir)
    if rc != 0:
        print(f"  [FAIL] `terraform apply -refresh-only` exited {rc}.")
        return False

    print("  Step 3/3 — re-verifying against cloud...")
    drift = _reverify(tf_address, state_path)
    _print_reverify_result(drift)
    if drift is not None and drift.has_drift:
        # Refresh-only pulled what terraform could pull, but our diff still
        # sees a difference — usually a field outside terraform's schema.
        print(f"  [WARN]  Accepted {tf_address}, but detector still reports drift.")
        print(f"      State updated as far as terraform could; HCL still diverged.")
        return True
    print(f"  [OK] Accepted {tf_address} (verified in sync). State updated; HCL still diverged.")
    return True


# --- Missing-cloud-resource handlers ------------------------------------
#
# When the cloud snapshot is None (resource deleted out-of-band), the diff
# engine sets drift.error and skips item-level diff. We can't Restore /
# Accept against a phantom — we either re-create from HCL or stop tracking
# it. Both directions are dangerous, so each is gated by typed-name confirm.

def _recreate(tf_address: str,
              *,
              confirmation: ConfirmationPolicy,
              policy_check: Optional[Callable[[], Any]] = None,
              workdir: Optional[str] = None) -> bool:
    """
    Cloud was deleted; re-create it via terraform from the HCL definition.

    `terraform apply -target=ADDR` will detect the missing resource and
    provision a fresh one. This can be expensive (full provisioning), and
    can fail or do the wrong thing if HCL has drifted beyond what cloud
    will accept (name collisions, region changes), so we gate it behind
    typed-name confirmation. Note: any data on the original (bucket
    objects, instance disk contents) is NOT recovered.

    `policy_check`: see `_restore` — same gate, fires after the typed
    confirm and before backup/apply. For Recreate the gate is arguably
    *more* important than for Restore: Recreate provisions a brand-new
    cloud resource, so a non-compliant HCL block goes from "doesn't
    exist in the cloud" to "exists and is non-compliant" in one step.
    """
    print("\n  Recreate action: re-create the deleted cloud resource from HCL.")
    print(f"      -> `terraform apply -target={tf_address} -auto-approve`")
    print("      Risk: this PROVISIONS a new cloud resource. Any data on the")
    print("      original (bucket objects, instance disk contents) is NOT recovered.")
    if not confirmation.typed(
        tf_address,
        f"\n  Type the address `{tf_address}` to confirm: ",
    ):
        return False

    # Pre-apply policy gate. See `_restore` for the rationale; same gate,
    # same fail-OPEN semantics, same default block_at="HIGH".
    if not _run_policy_gate(tf_address, policy_check, confirmation):
        return False

    state_path = _state_path(workdir=workdir)
    print("  Step 1/2 — backing up state and applying...")
    if _backup_state(state_path) is None:
        print("  [FAIL] Refusing to apply without a state backup.")
        return False
    rc = _run_terraform(["apply", f"-target={tf_address}", "-auto-approve"], cwd=workdir)
    if rc != 0:
        print(f"  [FAIL] `terraform apply` exited {rc}.")
        return False

    print("  Step 2/2 — re-verifying against cloud...")
    drift = _reverify(tf_address, state_path)
    _print_reverify_result(drift)
    if drift is not None and drift.has_drift:
        print(f"  [WARN]  Recreated {tf_address}, but cloud still differs from state.")
        return True
    print(f"  [OK] Recreated {tf_address} (verified in sync).")
    return True


def _drop(tf_address: str, *, confirmation: ConfirmationPolicy, workdir: Optional[str] = None) -> bool:
    """
    Cloud is gone for good; tell terraform to stop tracking it.

    `terraform state rm ADDR` removes the entry from terraform.tfstate. The
    HCL block is NOT removed — next `terraform plan` will see the resource
    as "to add" and offer to recreate it. The user should follow up by
    deleting the HCL block manually if the deletion was intentional.
    """
    print("\n  Drop action: remove the resource from terraform state.")
    print(f"      -> `terraform state rm {tf_address}`")
    print("      The HCL block is NOT removed. Next `terraform plan` will show")
    print("      the resource as 'to add'. Edit your .tf files to drop it for good.")
    if not confirmation.typed(
        tf_address,
        f"\n  Type the address `{tf_address}` to confirm: ",
    ):
        return False

    state_path = _state_path(workdir=workdir)
    print("  Step 1/1 — backing up state and removing from state...")
    if _backup_state(state_path) is None:
        print("  [FAIL] Refusing to mutate state without a backup.")
        return False
    rc = _run_terraform(["state", "rm", tf_address], cwd=workdir)
    if rc != 0:
        print(f"  [FAIL] `terraform state rm` exited {rc}.")
        return False
    print(f"  [OK] Dropped {tf_address} from state.")
    return True


def _remediate_missing(d: ResourceDrift,
                       *,
                       confirmation: ConfirmationPolicy,
                       policy_check: Optional[Callable[[], Any]] = None,
                       workdir: Optional[str] = None,
                       ) -> Tuple[str, bool]:
    """
    Drive the missing-cloud-resource branch. Returns (action, success):
        action ∈ {"recreated", "dropped", "skipped", "quit"}
        success: True iff the action ran cleanly (or the user explicitly
                 chose Skip, which is a "success" in the sense of being
                 the user's deliberate decision).

    The R/D/S/Q multi-choice prompt is CLI-only — programmatic callers
    pick the action upstream and dispatch via `remediate_one()` directly
    to `_recreate` or `_drop`, so this helper never appears in headless
    flows. Hence we keep the bare `_prompt(...)` here rather than
    routing through `confirmation`.

    `policy_check` is forwarded only to `_recreate` (Drop never mutates
    cloud, so the gate would be theatre and `_drop` doesn't accept the
    kwarg).
    """
    print()
    print("   Cloud resource is gone (deleted out-of-band).")
    print("   Choose action:")
    print("     [R] Recreate — push HCL back to cloud (creates a NEW resource)")
    print("     [D] Drop     — remove from state (HCL kept; next plan offers to add)")
    print("     [S] Skip     — leave the inconsistency in place")
    print("     [Q] Quit remediation")
    choice = _prompt("   > ", valid={"R", "D", "S", "Q"})

    if choice == "Q":
        return ("quit", False)
    if choice == "S":
        return ("skipped", True)
    if choice == "R":
        return ("recreated", _recreate(d.tf_address, confirmation=confirmation, policy_check=policy_check, workdir=workdir))
    # D
    return ("dropped", _drop(d.tf_address, confirmation=confirmation, workdir=workdir))


# --- Per-resource display ------------------------------------------------

def _print_resource_drift(drift: ResourceDrift) -> None:
    """Reprint the diff items for one resource at the remediation prompt."""
    if drift.error:
        print(f"   ERROR: {drift.error}")
        return
    for item in drift.items:
        if item.op == "added":
            print(f"   + {item.path}  (cloud-only)")
            print(f"       cloud: {item.cloud_value!r}")
        elif item.op == "removed":
            print(f"   - {item.path}  (state-only)")
            print(f"       state: {item.state_value!r}")
        else:
            print(f"   ~ {item.path}")
            print(f"       state: {item.state_value!r}")
            print(f"       cloud: {item.cloud_value!r}")


# --- Top-level driver ----------------------------------------------------

def run_remediation(
    drifts: List[ResourceDrift],
    *,
    confirmation: Optional[ConfirmationPolicy] = None,
    enable_policy_gate: bool = True,
    workdir: Optional[str] = None,
) -> RemediationSummary:
    """
    Walk the user through each drifted resource. Safe no-op if there's
    nothing drifted or if we're not on a tty.

    The `confirmation` kwarg controls how per-action gates (Y/N prompts,
    typed-name confirms) are answered. Default is `InteractivePolicy`,
    preserving the historical CLI behaviour including the non-tty bail-
    out. A caller that passes a non-interactive policy (e.g. the batch
    test harness supplying `AutoConfirmPolicy()`) opts out of the bail-
    out and drives the action choice itself via the CLI multi-choice
    prompts — which is rarely what you want. For UI / single-resource
    remediation use `remediate_one()` instead.

    `enable_policy_gate` (default True) wires the TODO #14 pre-apply
    policy gate into the Restore and Recreate paths. Set to False to
    bypass entirely (e.g. emergency hotfix when conftest is unavailable
    or known to be broken). Off by no-op when the policy module isn't
    installed — `_policy_check_for` fail-OPENs on ImportError. The CLI
    flow originally wired the gate ONLY into `remediate_one()`, leaving
    this driver silently un-gated; the live drift test caught the gap.
    """
    summary = RemediationSummary()
    drifted = [d for d in drifts if d.has_drift]
    if not drifted:
        return summary

    # Legacy tty bail-out fires only when no explicit policy was supplied;
    # callers that pass a policy have opted into headless behaviour.
    if confirmation is None:
        if not _is_interactive():
            print("\n(non-interactive shell detected; skipping remediation prompt)")
            return summary
        confirmation = InteractivePolicy()

    print("\n" + "=" * 70)
    print("REMEDIATION")
    print("=" * 70)
    answer = _prompt(
        f"{len(drifted)} resource(s) drifted. Walk through them now? [Y/n]: ",
        valid={"Y", "N"}, default="Y",
    )
    if answer != "Y":
        print("Remediation declined. Drift left in place.")
        return summary

    for i, d in enumerate(drifted, start=1):
        print("\n" + "-" * 70)
        print(f"[{i}/{len(drifted)}] {d.tf_address}")
        print("-" * 70)
        _print_resource_drift(d)

        # Build the per-resource policy checker once — passed only to the
        # cloud-mutating actions (_restore, _recreate). Accept and Drop
        # don't take it. The closure is lazy: it doesn't fetch the cloud
        # snapshot until the gate actually fires, so resources the user
        # picks Skip on incur zero policy-eval cost.
        pc: Optional[Callable[[], Any]] = None
        if enable_policy_gate:
            pc = _policy_check_for(d.tf_address, workdir=workdir)

        # Missing-cloud-resource: distinct flow with [R]ecreate / [D]rop. Both
        # are gated by typed-name confirmation inside the helpers. We fold the
        # outcomes into the existing summary buckets (recreate->restored,
        # drop->accepted) since they're semantically equivalent directions.
        if d.error:
            action, ok = _remediate_missing(d, confirmation=confirmation, policy_check=pc, workdir=workdir)
            if action == "quit":
                print("  Quitting remediation. Remaining resources left untouched.")
                break
            if action == "skipped":
                summary.skipped.append(d.tf_address)
                print(f"  [SKIP]  Skipped {d.tf_address}.")
                continue
            if action == "recreated":
                if ok:
                    summary.restored.append(d.tf_address)
                else:
                    summary.failed.append((d.tf_address, "recreate"))
            elif action == "dropped":
                if ok:
                    summary.accepted.append(d.tf_address)
                else:
                    summary.failed.append((d.tf_address, "drop"))
            continue

        print()
        print("   Choose action:")
        print("     [R] Restore — push state to cloud  (cloud reverts to recorded value)")
        print("     [A] Accept  — pull cloud into state (state updated; HCL not)")
        print("     [S] Skip    — leave drift in place")
        print("     [Q] Quit remediation")
        choice = _prompt("   > ", valid={"R", "A", "S", "Q"})

        if choice == "Q":
            print("  Quitting remediation. Remaining resources left untouched.")
            break
        if choice == "S":
            summary.skipped.append(d.tf_address)
            print(f"  [SKIP]  Skipped {d.tf_address}.")
            continue
        if choice == "R":
            if _restore(d.tf_address, confirmation=confirmation, policy_check=pc, workdir=workdir):
                summary.restored.append(d.tf_address)
            else:
                summary.failed.append((d.tf_address, "restore"))
        elif choice == "A":
            if _accept(d.tf_address, confirmation=confirmation, workdir=workdir):
                summary.accepted.append(d.tf_address)
            else:
                summary.failed.append((d.tf_address, "accept"))

    _print_summary(summary)
    return summary


# --- Programmatic single-resource API -----------------------------------
#
# The Streamlit UI (and the upcoming FastAPI layer) drives remediation one
# resource at a time: the user clicks Restore on a row, the backend runs
# the action for that one address, returns a structured verdict, the UI
# updates the row's status badge, and control returns to the human. This
# is fundamentally different from `run_remediation()`'s interactive loop —
# no walk-through, no action-choice prompt, no stdin.
#
# `remediate_one()` is the entry point for that flow. It:
#   1. Picks a confirmation policy (auto-confirm by default for programmatic
#      callers — the button click upstream IS the confirmation).
#   2. Dispatches to the matching action helper.
#   3. Wraps bool/exception outcomes in a `RemediationResult` dataclass so
#      the caller can render a status badge without parsing stdout.
#
# stdout prints still happen inside the helpers; the UI can surface them
# via log capture or just tail the container logs. Structured output is
# what we promise; pretty rendering is the caller's job.

_VALID_ACTIONS = ("restore", "accept", "recreate", "drop")


def remediate_one(
    tf_address: str,
    action: str,
    *,
    auto_confirm: bool = True,
    confirmation: Optional[ConfirmationPolicy] = None,
    enable_policy_gate: bool = True,
    cloud_snapshot: Optional[Dict[str, Any]] = None,
    policy_check: Optional[Callable[[], Any]] = None,
    workdir: Optional[str] = None,
) -> RemediationResult:
    """Run a single remediation action against a single resource.

    Args:
        tf_address:   Fully-qualified Terraform address, e.g.
                      ``google_storage_bucket.demo``.
        action:       One of ``"restore"``, ``"accept"``, ``"recreate"``,
                      ``"drop"``. Case-insensitive.
        auto_confirm: When True (the default — programmatic callers usually
                      want this), every prompt is pre-answered "Y" and
                      every typed-name confirm auto-passes. When False,
                      uses InteractivePolicy (stdin). Ignored if
                      `confirmation` is supplied explicitly.
        confirmation: Custom `ConfirmationPolicy` for callers that need
                      finer control (e.g. answer Y to apply prompts but
                      refuse typed-confirms). Overrides `auto_confirm`.
        enable_policy_gate: When True (default), Restore/Recreate evaluate
                      OPA/Conftest policy on the resource pre-apply and
                      block on HIGH+ violations unless `confirmation`
                      answers Y to the override prompt. Set False to
                      bypass entirely (e.g. tests, emergency hotfix).
                      No-op for Accept/Drop (cloud isn't mutated).
        cloud_snapshot: Pre-fetched GCP describe-JSON for the resource.
                      When provided, policy evaluation skips the redundant
                      cloud round-trip. Typically supplied by the detector
                      which already has the snapshot in hand.
        policy_check: Pre-built closure for policy evaluation. When
                      provided, `enable_policy_gate` and `cloud_snapshot`
                      are ignored — used by tests to inject deterministic
                      `PolicyImpact` results.
        workdir:      Per-project working directory (absolute path) where
                      terraform.tfstate lives. Programmatic / SaaS callers
                      MUST pass this -- it's the only thread-safe way to
                      target a specific project's state when multiple
                      requests are in flight in the same Python process.
                      When None, falls back to ``os.getcwd()``, which is
                      what the CLI relies on (detector/run.py chdirs to
                      the chosen workdir at entry).

    Returns:
        `RemediationResult` with `success` set iff the action ran clean
        (same semantics as the underlying helpers' bool returns). `status`
        is one of: ``"ok"``, ``"failed"``, ``"invalid_action"``,
        ``"exception"``. Exceptions from the helpers are caught and turned
        into result objects — programmatic callers should not have to wrap
        this in try/except.
    """
    if confirmation is None:
        confirmation = AutoConfirmPolicy() if auto_confirm else InteractivePolicy()

    action_key = action.lower()
    handler = {
        "restore":  _restore,
        "accept":   _accept,
        "recreate": _recreate,
        "drop":     _drop,
    }.get(action_key)
    if handler is None:
        return RemediationResult(
            tf_address=tf_address,
            action=action,
            success=False,
            status="invalid_action",
            message=f"Unknown action {action!r}; expected one of {list(_VALID_ACTIONS)}.",
        )

    # Build the policy checker only for actions that mutate cloud
    # (restore/recreate). Accept/Drop never touch cloud, so a gate there
    # would be theatre — and would also crash on the unsupported kwarg.
    pc: Optional[Callable[[], Any]] = None
    if action_key in ("restore", "recreate") and enable_policy_gate:
        pc = policy_check or _policy_check_for(tf_address, cloud_snap=cloud_snapshot, workdir=workdir)

    try:
        if action_key in ("restore", "recreate"):
            ok = handler(tf_address, confirmation=confirmation, policy_check=pc, workdir=workdir)
        else:
            ok = handler(tf_address, confirmation=confirmation, workdir=workdir)
    except Exception as e:  # pragma: no cover — defensive shell for the UI
        # P4-1: keep the broad catch (this is the documented UI defensive
        # shell -- callers receive a typed RemediationResult instead of
        # an unhandled exception bubbling into Streamlit / FastAPI), but
        # ALSO print the traceback to stderr so operators reading the
        # process log see the original failure. Pre-P4-1 the stack was
        # silently dropped on the floor; only the formatted message
        # survived, making real bugs hard to diagnose.
        #
        # When CC-1 detector logging migration ships, replace this with
        # ``_log.exception("remediate_one_unhandled_exception",
        # tf_address=tf_address, action=action_key)`` which serializes
        # the exception + stack into the structured log automatically.
        traceback.print_exc(file=sys.stderr)
        return RemediationResult(
            tf_address=tf_address,
            action=action_key,
            success=False,
            status="exception",
            message=f"{type(e).__name__}: {e}",
        )

    return RemediationResult(
        tf_address=tf_address,
        action=action_key,
        success=ok,
        status="ok" if ok else "failed",
    )


def _print_summary(summary: RemediationSummary) -> None:
    print("\n" + "=" * 70)
    print("REMEDIATION SUMMARY")
    print("=" * 70)
    print(f"  [OK] Restored: {len(summary.restored)}")
    for addr in summary.restored:
        print(f"       {addr}")
    print(f"  [ACCEPT] Accepted: {len(summary.accepted)}")
    for addr in summary.accepted:
        print(f"       {addr}")
    print(f"  [SKIP]  Skipped:  {len(summary.skipped)}")
    for addr in summary.skipped:
        print(f"       {addr}")
    if summary.failed:
        print(f"  [FAIL] Failed:   {len(summary.failed)}")
        for addr, op in summary.failed:
            print(f"       {addr} ({op})")
    print("=" * 70)
