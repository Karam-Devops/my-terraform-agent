# detector/remediator.py
"""
Interactive drift remediation.

After the diff_engine produces a report, this module walks the user through
each drifted resource and offers per-resource actions.

For *modified* drift (state and cloud differ on a tracked field):
    [R]estore  — push recorded state back to cloud
                 (terraform plan -target=ADDR  →  apply -target=ADDR -auto-approve)
    [A]ccept   — pull cloud changes into state, leaving cloud + HCL untouched
                 (terraform plan -refresh-only -target=ADDR  →  apply -refresh-only ...)
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
    and a falsely cheerful "✅ Restored" report.

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
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import List, Optional, Tuple

from . import cloud_snapshot, config, state_reader
from .diff_engine import ResourceDrift, diff_resource


# --- Summary record -------------------------------------------------------

@dataclass
class RemediationSummary:
    restored: List[str] = field(default_factory=list)
    accepted: List[str] = field(default_factory=list)
    skipped: List[str] = field(default_factory=list)
    failed: List[Tuple[str, str]] = field(default_factory=list)  # (addr, op)


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


def _run_terraform(args: List[str]) -> int:
    """
    Stream a terraform invocation to stdout, return its exit code.

    We stream (Popen + iterate stdout) rather than capture (run + print)
    so the user sees plan output flow in real time during a demo. Slower
    feedback breaks the closed-loop feel.

    Two non-obvious behaviours baked in:
      - `-input=false` is forced for plan/apply/refresh-family commands.
        Terraform parses ALL `.tf` files in the workspace before honouring
        -target, and if any declare a variable with no default (very common
        in translator output that lives in the same directory) it goes
        interactive and HANGS the remediator on a prompt. -input=false makes
        that fail fast with a clear error. We do NOT inject this for `state`
        subcommands, which reject unknown flags after the subcommand name.
      - `terraform` not on PATH is reported clearly rather than crashing.
    """
    if args and args[0] in _INPUT_FALSE_COMMANDS:
        full_args = [args[0], "-input=false"] + list(args[1:])
    else:
        full_args = list(args)
    pretty = "terraform " + " ".join(full_args)
    print(f"  → Running: {pretty}")
    try:
        proc = subprocess.Popen(
            ["terraform"] + full_args,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            # Force UTF-8 decoding of terraform's stdout. Without this, Python
            # falls back to the system locale (cp1252 on Windows), which
            # mangles terraform's Unicode box-drawing chars (╷│╵) into mojibake.
            encoding="utf-8",
            errors="replace",
        )
    except FileNotFoundError:
        print("  ❌ `terraform` not found on PATH. Action skipped.")
        return 127

    assert proc.stdout is not None  # for type-checkers; Popen with PIPE guarantees this
    for line in proc.stdout:
        print(f"    {line.rstrip()}")
    proc.wait()
    return proc.returncode


# --- Safety rails (state backup, post-apply re-verification) ------------

def _state_path() -> str:
    """Resolve the active terraform.tfstate path. Mirrors run.py's logic so
    we can locate the file even when the caller didn't pass it through."""
    project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    return os.path.join(project_root, config.STATE_FILE_NAME)


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
        print(f"  ⚠️  Cannot back up state — file not found at {state_path}.")
        return None

    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_path = f"{state_path}.backup-{ts}"
    try:
        shutil.copy2(state_path, backup_path)
    except (OSError, IOError) as e:
        print(f"  ⚠️  State backup failed: {e}")
        return None
    print(f"  💾 State backed up → {os.path.basename(backup_path)}")
    return backup_path


def _reverify(tf_address: str, state_path: str) -> Optional[ResourceDrift]:
    """
    Re-read state, re-fetch cloud, re-diff the single named resource.

    Returns the fresh ResourceDrift, or None if we couldn't even run the
    verification (e.g., the resource is no longer in state — expected after
    a `terraform state rm`).

    Reverify failure is non-fatal: the caller's apply may have succeeded
    regardless. The point of this function is to *report honestly* —
    distinguish "✅ Restored AND verified in sync" from "✅ apply ran clean
    BUT cloud still differs". Without it, our previous false-success bug
    (apply-as-no-op on unmanaged fields) silently shipped wrong reports.
    """
    resources = state_reader.read_state(state_path)
    target = next((r for r in resources if r.tf_address == tf_address), None)
    if target is None:
        # Expected after a successful `state rm`. Not an error.
        print(f"  ℹ️  {tf_address} is no longer present in state — nothing to re-diff.")
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
        print(f"  ⚠️  Re-verify: {drift.error}")
        return
    if not drift.has_drift:
        print(f"  ✅ Re-verify: {drift.tf_address} is in sync with cloud.")
        return
    n = len(drift.items)
    print(f"  ⚠️  Re-verify: drift remains on {drift.tf_address} ({n} item(s)):")
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
    print(f"  ⏭  Confirmation did not match (expected '{expected}'). Aborted.")
    return False


# --- Per-resource action handlers ---------------------------------------

def _restore(tf_address: str) -> bool:
    """
    Cloud ← state. Plan → backup → apply → re-verify.

    Returns True iff apply succeeded. Any failure (plan failure, no-op
    detected, user declines, backup failure, apply failure) returns False
    so the caller can record it in the summary. Residual drift after a
    successful apply is reported as success-with-warning, not failure —
    the apply did do its job; the gap is honest reporting.

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
    rc = _run_terraform(["plan", f"-target={tf_address}", "-detailed-exitcode"])
    if rc == 1:
        print(f"  ❌ `terraform plan` errored (exit 1). Apply not attempted.")
        return False
    if rc == 0:
        # No-change plan — Restore via terraform is impossible for this drift.
        print()
        print("  ⚠️  terraform plan reports NO changes to make.")
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
        print(f"  ❌ `terraform plan` returned unexpected exit code {rc}. Apply not attempted.")
        return False

    answer = _prompt(
        "\n  Proceed with apply? [y/N]: ",
        valid={"Y", "N"}, default="N",
    )
    if answer != "Y":
        print("  ⏭  Apply skipped.")
        return False

    state_path = _state_path()
    print("  Step 2/3 — backing up state and applying...")
    if _backup_state(state_path) is None:
        print("  ❌ Refusing to apply without a state backup.")
        return False
    rc = _run_terraform(["apply", f"-target={tf_address}", "-auto-approve"])
    if rc != 0:
        print(f"  ❌ `terraform apply` exited {rc}.")
        return False

    print("  Step 3/3 — re-verifying against cloud...")
    drift = _reverify(tf_address, state_path)
    _print_reverify_result(drift)
    if drift is not None and drift.has_drift:
        # Apply ran clean but cloud still differs: don't lie about full closure.
        print(f"  ⚠️  Restored {tf_address}, but cloud still differs from state.")
        return True
    print(f"  ✅ Restored {tf_address} (verified in sync).")
    return True


def _accept(tf_address: str) -> bool:
    """
    State ← cloud. Refresh-only plan → backup → refresh-only apply → re-verify.

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
    ])
    if rc == 1:
        print(f"  ❌ `terraform plan -refresh-only` errored (exit 1). Apply not attempted.")
        return False
    if rc == 0:
        print()
        print("  ⚠️  terraform plan -refresh-only reports NO changes to make.")
        print("      Cloud already matches what terraform reads back into state.")
        print("      The drift the detector reports may live in a field terraform")
        print("      doesn't track in its schema, or may be a detector false positive.")
        return False
    if rc != 2:
        print(f"  ❌ `terraform plan -refresh-only` returned unexpected exit code {rc}.")
        return False

    answer = _prompt(
        "\n  Proceed with refresh-only apply? [y/N]: ",
        valid={"Y", "N"}, default="N",
    )
    if answer != "Y":
        print("  ⏭  Apply skipped.")
        return False

    state_path = _state_path()
    print("  Step 2/3 — backing up state and applying refresh-only...")
    if _backup_state(state_path) is None:
        print("  ❌ Refusing to apply without a state backup.")
        return False
    rc = _run_terraform([
        "apply", "-refresh-only", f"-target={tf_address}", "-auto-approve",
    ])
    if rc != 0:
        print(f"  ❌ `terraform apply -refresh-only` exited {rc}.")
        return False

    print("  Step 3/3 — re-verifying against cloud...")
    drift = _reverify(tf_address, state_path)
    _print_reverify_result(drift)
    if drift is not None and drift.has_drift:
        # Refresh-only pulled what terraform could pull, but our diff still
        # sees a difference — usually a field outside terraform's schema.
        print(f"  ⚠️  Accepted {tf_address}, but detector still reports drift.")
        print(f"      State updated as far as terraform could; HCL still diverged.")
        return True
    print(f"  ✅ Accepted {tf_address} (verified in sync). State updated; HCL still diverged.")
    return True


# --- Missing-cloud-resource handlers ------------------------------------
#
# When the cloud snapshot is None (resource deleted out-of-band), the diff
# engine sets drift.error and skips item-level diff. We can't Restore /
# Accept against a phantom — we either re-create from HCL or stop tracking
# it. Both directions are dangerous, so each is gated by typed-name confirm.

def _recreate(tf_address: str) -> bool:
    """
    Cloud was deleted; re-create it via terraform from the HCL definition.

    `terraform apply -target=ADDR` will detect the missing resource and
    provision a fresh one. This can be expensive (full provisioning), and
    can fail or do the wrong thing if HCL has drifted beyond what cloud
    will accept (name collisions, region changes), so we gate it behind
    typed-name confirmation. Note: any data on the original (bucket
    objects, instance disk contents) is NOT recovered.
    """
    print("\n  Recreate action: re-create the deleted cloud resource from HCL.")
    print(f"      → `terraform apply -target={tf_address} -auto-approve`")
    print("      Risk: this PROVISIONS a new cloud resource. Any data on the")
    print("      original (bucket objects, instance disk contents) is NOT recovered.")
    if not _typed_confirm(
        tf_address,
        f"\n  Type the address `{tf_address}` to confirm: ",
    ):
        return False

    state_path = _state_path()
    print("  Step 1/2 — backing up state and applying...")
    if _backup_state(state_path) is None:
        print("  ❌ Refusing to apply without a state backup.")
        return False
    rc = _run_terraform(["apply", f"-target={tf_address}", "-auto-approve"])
    if rc != 0:
        print(f"  ❌ `terraform apply` exited {rc}.")
        return False

    print("  Step 2/2 — re-verifying against cloud...")
    drift = _reverify(tf_address, state_path)
    _print_reverify_result(drift)
    if drift is not None and drift.has_drift:
        print(f"  ⚠️  Recreated {tf_address}, but cloud still differs from state.")
        return True
    print(f"  ✅ Recreated {tf_address} (verified in sync).")
    return True


def _drop(tf_address: str) -> bool:
    """
    Cloud is gone for good; tell terraform to stop tracking it.

    `terraform state rm ADDR` removes the entry from terraform.tfstate. The
    HCL block is NOT removed — next `terraform plan` will see the resource
    as "to add" and offer to recreate it. The user should follow up by
    deleting the HCL block manually if the deletion was intentional.
    """
    print("\n  Drop action: remove the resource from terraform state.")
    print(f"      → `terraform state rm {tf_address}`")
    print("      The HCL block is NOT removed. Next `terraform plan` will show")
    print("      the resource as 'to add'. Edit your .tf files to drop it for good.")
    if not _typed_confirm(
        tf_address,
        f"\n  Type the address `{tf_address}` to confirm: ",
    ):
        return False

    state_path = _state_path()
    print("  Step 1/1 — backing up state and removing from state...")
    if _backup_state(state_path) is None:
        print("  ❌ Refusing to mutate state without a backup.")
        return False
    rc = _run_terraform(["state", "rm", tf_address])
    if rc != 0:
        print(f"  ❌ `terraform state rm` exited {rc}.")
        return False
    print(f"  ✅ Dropped {tf_address} from state.")
    return True


def _remediate_missing(d: ResourceDrift) -> Tuple[str, bool]:
    """
    Drive the missing-cloud-resource branch. Returns (action, success):
        action ∈ {"recreated", "dropped", "skipped", "quit"}
        success: True iff the action ran cleanly (or the user explicitly
                 chose Skip, which is a "success" in the sense of being
                 the user's deliberate decision).
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
        return ("recreated", _recreate(d.tf_address))
    # D
    return ("dropped", _drop(d.tf_address))


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

def run_remediation(drifts: List[ResourceDrift]) -> RemediationSummary:
    """
    Walk the user through each drifted resource. Safe no-op if there's
    nothing drifted or if we're not on a tty.
    """
    summary = RemediationSummary()
    drifted = [d for d in drifts if d.has_drift]
    if not drifted:
        return summary

    if not _is_interactive():
        print("\n(non-interactive shell detected; skipping remediation prompt)")
        return summary

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

        # Missing-cloud-resource: distinct flow with [R]ecreate / [D]rop. Both
        # are gated by typed-name confirmation inside the helpers. We fold the
        # outcomes into the existing summary buckets (recreate→restored,
        # drop→accepted) since they're semantically equivalent directions.
        if d.error:
            action, ok = _remediate_missing(d)
            if action == "quit":
                print("  Quitting remediation. Remaining resources left untouched.")
                break
            if action == "skipped":
                summary.skipped.append(d.tf_address)
                print(f"  ⏭  Skipped {d.tf_address}.")
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
            print(f"  ⏭  Skipped {d.tf_address}.")
            continue
        if choice == "R":
            if _restore(d.tf_address):
                summary.restored.append(d.tf_address)
            else:
                summary.failed.append((d.tf_address, "restore"))
        elif choice == "A":
            if _accept(d.tf_address):
                summary.accepted.append(d.tf_address)
            else:
                summary.failed.append((d.tf_address, "accept"))

    _print_summary(summary)
    return summary


def _print_summary(summary: RemediationSummary) -> None:
    print("\n" + "=" * 70)
    print("REMEDIATION SUMMARY")
    print("=" * 70)
    print(f"  ✅ Restored: {len(summary.restored)}")
    for addr in summary.restored:
        print(f"       {addr}")
    print(f"  📥 Accepted: {len(summary.accepted)}")
    for addr in summary.accepted:
        print(f"       {addr}")
    print(f"  ⏭  Skipped:  {len(summary.skipped)}")
    for addr in summary.skipped:
        print(f"       {addr}")
    if summary.failed:
        print(f"  ❌ Failed:   {len(summary.failed)}")
        for addr, op in summary.failed:
            print(f"       {addr} ({op})")
    print("=" * 70)
