# my-terraform-agent/importer/terraform_client.py

import json
import re
import subprocess
import os
import tempfile
from . import config


# ---------------------------------------------------------------------------
# Path 1: cross-file error attribution
# ---------------------------------------------------------------------------
#
# `terraform plan -target=X` (PR-13) scopes the *diff* to one resource, but
# Terraform still parses every .tf file in the working directory before any
# plan can run. If a sibling file has a config-load error (`Unsupported
# argument`, `Unsupported block type`, etc.), every resource's `-target`
# plan fails with the SAME error message — pointing at the broken sibling,
# not at the resource we're verifying.
#
# Pre-Path-1 behavior: each blocked sibling appeared in the failure menu
# with the cluster's error message. The operator saw the same error against
# 3 different resource names and (rationally) skipped them all, getting
# "0 / 3 resources imported successfully" even though the imports succeeded.
#
# `extract_error_files()` parses the standard `on FILENAME line N` prefix
# Terraform attaches to every error block so the failure-correction loop
# can classify failures as SELF_BROKEN vs BLOCKED_BY_SIBLING and re-verify
# blocked siblings after the cause is fixed.

_ERROR_FILE_RE = re.compile(r"on\s+([^\s,]+\.tf)\s+line", re.IGNORECASE)


def extract_error_files(plan_output):
    """Extract .tf filenames mentioned in Terraform error output.

    Returns a deduplicated list (in order of first appearance) of every
    filename Terraform attributed errors to. Empty list if no `on X.tf
    line N` markers are present (e.g., pure runtime errors with no source
    location).
    """
    if not plan_output:
        return []
    return list(dict.fromkeys(_ERROR_FILE_RE.findall(plan_output)))


def _ensure_initialized(workdir=None):
    """Internal helper: checks if Terraform is initialized in `workdir`; runs init if not.

    Per-project workdir refactor: paths are now resolved relative to the
    explicit workdir, NOT the process cwd. Falls back to process cwd if
    workdir is None purely for back-compat with any caller still pre-dating
    the refactor (none in tree, but defensive).
    """
    base = workdir or os.getcwd()
    if not os.path.isdir(os.path.join(base, ".terraform")) or not os.path.isfile(os.path.join(base, ".terraform.lock.hcl")):
        print(f"   - ⚠️ Terraform plugins missing or lock file inconsistent in {base}. Auto-initializing...")
        # Force an upgrade to ensure the lock file is written correctly for the current .tf files
        return init(workdir=workdir, upgrade=True)
    return True

def init(workdir=None, upgrade=False):
    """Runs 'terraform init' inside `workdir` (or process cwd if None).

    Per-project workdir refactor: every terraform command runs with
    `cwd=workdir` so the .terraform/ plugin dir, terraform.tfstate, and
    .terraform.lock.hcl all live alongside the .tf files for THIS project,
    not commingled at the repo root.

    Canonical-lock seeding: before init runs, we copy the committed
    `provider_versions/.terraform.lock.hcl` into the workdir if it
    doesn't already have one. This makes every project (yours, demo,
    future SaaS client) resolve provider versions identically without
    requiring per-project lock files to be committed -- which would not
    work in a multi-tenant context anyway. The seed is a no-op when:
      * upgrade=True (operator wants a fresh resolve, by definition)
      * workdir already has a lock file (operator's pin wins)
      * canonical seed is absent (clean fallback to registry resolution)
    Lazy-import keeps importer/ decoupled from common/workdir at module
    load time (matches the pattern used in schema_oracle.py).
    """
    print(f"\n--- {'Re-initializing' if upgrade else 'Initializing'} Terraform in {workdir or os.getcwd()} ---")
    if not upgrade and workdir:
        try:
            from common.workdir import seed_lock_file
            if seed_lock_file(workdir):
                print(f"   - 🔒 Seeded canonical .terraform.lock.hcl into {workdir}")
        except OSError as seed_err:
            # Non-fatal: terraform init will create a fresh lock if the
            # seed copy failed (permissions, disk). Log so it's visible.
            print(f"   - ⚠️ Could not seed lock file ({seed_err}); init will resolve fresh.")
    command_args = [config.TERRAFORM_PATH, "init"]
    if upgrade:
        command_args.append("-upgrade")
    try:
        # Use subprocess.run directly as we don't need the complex file-redirection here
        subprocess.run(command_args, check=True, capture_output=True, text=True, cwd=workdir)
        return True
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        error_output = e.stderr if hasattr(e, 'stderr') and e.stderr else str(e)
        print(f"❌ Terraform init failed. Error: {error_output}")
        return False

def import_resource(mapping, force_refresh=False):
    """Runs 'terraform import', ensuring initialization first.

    Per-project workdir refactor: pulls workdir from the mapping dict
    (set by run.py's _map_asset_to_terraform). Every subprocess gets
    cwd=workdir so the import lands in the per-project terraform.tfstate,
    not the (now-deleted) commingled one at repo root.
    """
    workdir = mapping.get("workdir")
    if not _ensure_initialized(workdir=workdir):
        print(f"❌ Aborting import for '{mapping['resource_name']}' due to initialization failure.")
        return False

    tf_address = f'{mapping["tf_type"]}.{mapping["hcl_name"]}'

    if force_refresh:
        print(f"\n   - 🧹 Forcing state refresh for '{mapping['resource_name']}'...")
        remove_args = [config.TERRAFORM_PATH, "state", "rm", tf_address]
        try:
            subprocess.run(remove_args, capture_output=True, text=True, cwd=workdir)
        except Exception:
            pass

    print(f"\n--- Importing '{mapping['resource_name']}' (cwd={workdir}) ---")
    import_args = [config.TERRAFORM_PATH, "import", tf_address, mapping["import_id"]]
    try:
        subprocess.run(import_args, check=True, capture_output=True, text=True, cwd=workdir)
        print(f"✅ Import successful for '{mapping['resource_name']}'.")
        return True
    except subprocess.CalledProcessError as e:
        error_output = e.stderr if e.stderr else e.stdout

        if "Resource already managed by Terraform" in error_output and not force_refresh:
            print(f"✅ Resource '{mapping['resource_name']}' is already managed in state. Skipping import.")
            return True

        # Extract just the first line for cleaner logging
        first_line = error_output.splitlines()[0] if error_output else "Unknown Error"
        print(f"❌ Terraform import failed for '{mapping['resource_name']}'. Error: {first_line}")
        return False


# ---------------------------------------------------------------------------
# Plan classification (PR-13: per-resource verification + auto-reconcile)
# ---------------------------------------------------------------------------
#
# The previous `plan_for_resource(filename)` ignored its argument and ran
# `terraform plan` against the entire working directory. Two consequences:
#
#   1. One resource's drift contaminated every other resource's verdict —
#      multi-resource imports turned successful sibling resources into
#      false-positive failures, populated the correction queue with stale
#      cached errors, and made the importer untrustworthy in front of a
#      vendor.
#
#   2. Any non-zero diff was treated as failure, including the textbook
#      post-import "state catch-up" pattern (cloud has the value, state
#      hasn't recorded it yet, applying once reconciles silently). The
#      operator was forced to drop to a shell and run `terraform apply`
#      themselves.
#
# Both issues are fixed here:
#
#   * `terraform plan -target=<addr> -out=<plan_file>` scopes the diff to
#     the resource we actually care about (and its dependency closure).
#     `-out` saves the plan so we can both inspect it via `terraform show
#     -json` AND apply it without re-planning.
#
#   * `terraform show -json <plan_file>` gives us a structured diff to
#     classify against a small set of safe rules (see `_classify_plan`).
#     We never parse the human-readable plan text — that's brittle and
#     locale-sensitive.
#
#   * Pure-addition diffs (state has null/absent for every changed field;
#     HCL declares values that match the cloud snapshot) are auto-applied
#     using the saved plan file. Anything else — a `~` value mutation, a
#     `-` removal, a destroy/recreate — is FAIL and falls through to the
#     existing LLM correction loop.
#
# The auto-apply path is intentionally narrow. We never push hallucinated
# values to the cloud — only acknowledge values the cloud already has.

def _is_pure_addition(before, after):
    """True iff transitioning from `before` to `after` only ADDS values.

    A "pure addition" change satisfies all of:
      * no scalar value changes (no `before=X` becoming `after=Y` where X != Y)
      * no fields/elements removed
      * type doesn't change
      * the only differences are previously-null/absent values gaining content

    This is the only diff shape we'll auto-apply. It's the post-import
    state-catch-up pattern: the cloud already has the value, refresh just
    didn't surface it, the apply is a no-op at the API level.
    """
    if before is None or before == {} or before == []:
        # Adding to nothing is always pure addition.
        return True
    if after is None or after == {} or after == []:
        # Removing content is NEVER pure addition.
        return False
    if type(before) != type(after):
        # Type change is a value mutation, not an addition.
        return False
    if isinstance(after, dict):
        for key, before_v in before.items():
            after_v = after.get(key)
            if not _is_pure_addition(before_v, after_v):
                return False
        return True
    if isinstance(after, list):
        if len(before) > len(after):
            return False  # removed elements
        for b, a in zip(before, after):
            if not _is_pure_addition(b, a):
                return False
        return True
    # Scalar: must match exactly.
    return before == after


def _summarize_additions(before, after, prefix=""):
    """Return a list of dotted paths describing the fields that gained values.

    Used for transparent logging during AUTO-RECONCILE so the operator can
    see exactly what's being committed (and the vendor's auditor can later).
    """
    paths = []
    if isinstance(after, dict):
        for k, after_v in after.items():
            sub = f"{prefix}.{k}" if prefix else k
            before_v = before.get(k) if isinstance(before, dict) else None
            if before_v is None and after_v not in (None, {}, []):
                paths.append(sub)
            elif isinstance(after_v, (dict, list)) and before_v is not None:
                paths.extend(_summarize_additions(before_v, after_v, sub))
    elif isinstance(after, list) and isinstance(before, list):
        for i, a in enumerate(after):
            b = before[i] if i < len(before) else None
            paths.extend(_summarize_additions(b, a, f"{prefix}[{i}]"))
    return paths


def _classify_plan(plan_json, tf_address):
    """Classify the structured plan for a single resource address.

    Returns one of:
      "PASS"       — no changes recorded for this resource
      "AUTO_APPLY" — only `update` actions, all changes are pure additions
      "FAIL"       — anything else (real diff, recreate, delete, etc.)
    """
    changes = [
        c for c in plan_json.get("resource_changes", [])
        if c.get("address") == tf_address
    ]
    if not changes:
        return "PASS"
    for c in changes:
        actions = c.get("change", {}).get("actions") or []
        if actions == ["no-op"]:
            continue
        if actions != ["update"]:
            # create / delete / replace / read — never safe to auto-apply.
            return "FAIL"
        before = c.get("change", {}).get("before") or {}
        after = c.get("change", {}).get("after") or {}
        if not _is_pure_addition(before, after):
            return "FAIL"
    return "AUTO_APPLY"


def _run_show_json(plan_file, workdir=None):
    """Convert a saved plan file to structured JSON via `terraform show`.

    `workdir` MUST match the dir where the plan was produced -- `terraform
    show` resolves state and providers relative to cwd, and a plan file
    captured under project-A's workdir will fail to render under project-B.

    Returns the parsed dict, or None if anything goes wrong (we fall back
    to FAIL classification, which is the safe default).
    """
    show_args = [config.TERRAFORM_PATH, "show", "-json", plan_file]
    try:
        result = subprocess.run(show_args, capture_output=True, text=True, check=True, cwd=workdir)
        return json.loads(result.stdout)
    except (subprocess.CalledProcessError, json.JSONDecodeError) as e:
        print(f"   - WARN: `terraform show -json` failed ({e}); cannot classify structurally.")
        return None


def _apply_saved_plan(plan_file, workdir=None):
    """Commit a saved plan via `terraform apply <plan_file>`.

    `workdir` MUST match the dir where the plan was produced (see
    `_run_show_json` for why).

    Saved plans don't prompt for confirmation — that's the whole point of
    the save-and-apply pattern. Returns (ok, output_text).
    """
    apply_args = [
        config.TERRAFORM_PATH, "apply",
        "-no-color", "-input=false",
        plan_file,
    ]
    try:
        result = subprocess.run(apply_args, capture_output=True, text=True, check=True, cwd=workdir)
        return True, (result.stdout or "")
    except subprocess.CalledProcessError as e:
        return False, (e.stderr or e.stdout or str(e))


def plan_for_resource(mapping):
    """Verify a single imported resource against cloud reality.

    Runs `terraform plan -target=<addr> -out=<plan_file>` so the diff is
    scoped to one resource (its drift can't contaminate sibling resources'
    verdicts), then classifies via `terraform show -json` and auto-applies
    the safe post-import state-catch-up pattern.

    Returns (is_success: bool, plan_text: str). is_success=True for both
    PASS and AUTO_APPLY (after the apply succeeds). FAIL falls through to
    the existing LLM correction loop unchanged.
    """
    workdir = mapping.get("workdir")
    if not _ensure_initialized(workdir=workdir):
        return (False, "CRITICAL: Terraform failed to initialize. Cannot run plan.")

    tf_address = f'{mapping["tf_type"]}.{mapping["hcl_name"]}'
    print(f"\n--- Verifying '{tf_address}' (scoped plan via -target, cwd={workdir}) ---")

    plan_file = None
    output_file = None
    try:
        # Plan file holds the saved binary plan; output_file captures stdout/stderr
        # so we can print friendly text on failure without holding it all in memory.
        with tempfile.NamedTemporaryFile(suffix=".tfplan", delete=False) as pf:
            plan_file = pf.name
        with tempfile.NamedTemporaryFile(mode='w+', delete=False, encoding='utf-8') as of:
            output_file = of.name

        plan_args = [
            config.TERRAFORM_PATH, "plan",
            "-target", tf_address,
            "-out", plan_file,
            "-no-color",
            "-input=false",
        ]
        with open(output_file, 'w', encoding='utf-8') as f_out:
            process = subprocess.run(plan_args, stdout=f_out, stderr=f_out, cwd=workdir)
        with open(output_file, 'r', encoding='utf-8') as f_in:
            output = f_in.read()

        if process.returncode != 0:
            print(f"   - ❌ FAIL: `terraform plan` exited non-zero for '{tf_address}'.")
            return (False, output)

        # Fast path: plan text says "No changes" — no need to show -json.
        if "No changes. Your infrastructure matches the configuration." in output:
            print(f"   - ✅ PASS: '{tf_address}' matches cloud reality.")
            return (True, "Plan successful: No changes.")

        # Structural classification. Fall back to FAIL if show -json breaks.
        plan_json = _run_show_json(plan_file, workdir=workdir)
        if plan_json is None:
            print(f"   - ❌ FAIL: '{tf_address}' has changes but classification unavailable.")
            return (False, output)

        verdict = _classify_plan(plan_json, tf_address)

        if verdict == "PASS":
            # Plan exited 0 but the structured diff has no changes for OUR
            # resource (the textual 'No changes' line wasn't present because
            # `-target` may have pulled in dependency resources whose own
            # diffs show in the text). Treat as PASS.
            print(f"   - ✅ PASS: '{tf_address}' has no diff (sibling resources may differ).")
            return (True, "Plan successful: No changes for this resource.")

        if verdict == "AUTO_APPLY":
            # Identify the fields being added so the operator sees what's
            # about to be committed (visible audit trail in the log).
            our_changes = [
                c for c in plan_json.get("resource_changes", [])
                if c.get("address") == tf_address
            ]
            added_paths = []
            for c in our_changes:
                before = c.get("change", {}).get("before") or {}
                after = c.get("change", {}).get("after") or {}
                added_paths.extend(_summarize_additions(before, after))

            print(f"   - 🔄 AUTO-RECONCILE: '{tf_address}' has post-import state catch-up.")
            if added_paths:
                print(f"     Fields being acknowledged (cloud already has these values):")
                for p in added_paths[:10]:
                    print(f"       + {p}")
                if len(added_paths) > 10:
                    print(f"       ... and {len(added_paths) - 10} more")

            ok, apply_out = _apply_saved_plan(plan_file, workdir=workdir)
            if not ok:
                print(f"   - ❌ FAIL: auto-apply failed for '{tf_address}'.")
                combined = output + "\n\n--- AUTO-APPLY ERROR ---\n" + apply_out
                return (False, combined)

            print(f"   - ✅ AUTO-RECONCILED: '{tf_address}' state caught up to cloud.")
            return (True, "Plan successful: state auto-reconciled to cloud reality.")

        # FAIL: real diff that needs human / LLM review.
        print(f"   - ❌ FAIL: '{tf_address}' has a real diff that needs review.")
        return (False, output)

    finally:
        for f in (plan_file, output_file):
            if f and os.path.exists(f):
                try:
                    os.remove(f)
                except OSError:
                    pass
