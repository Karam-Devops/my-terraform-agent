# importer/quarantine.py
"""CG-7: failure-isolation via quarantine pattern.

The problem this solves: ``terraform plan`` parses every ``.tf`` file
in the workdir BEFORE honouring ``-target``, so one broken ``.tf``
cascades and blocks plan verification on every other resource. The
existing CLI flow (importer/run.py) classifies failures into
``self_broken`` vs ``blocked-by-sibling`` and offers an interactive
3-option HITL menu (snippet / AI self-correct / skip) for the
operator to fix the broken ones one at a time. That UX is fine for
the operator team's local debugging but wrong for a customer-facing
SaaS surface -- customers don't write HCL, don't read terraform
errors, and expect "13 of 16 imported, 3 need review" not "fix this
one before the others can be verified".

The quarantine pattern:

  1. After per-resource plan verification, every self-broken
     resource gets MAX_LLM_RETRIES rounds of AI self-correct
     SILENTLY (the existing option [2] path; just headless).

  2. If after the retries the resource STILL fails plan, its
     ``.tf`` file is moved to ``<workdir>/_quarantine/`` and its
     terraform state entry is removed via ``terraform state rm``.
     The resource lands in the WorkflowResult's ``needs_attention``
     bucket -- visible to the customer with a plain-English
     "couldn't be auto-translated" message and a 'review' action.

  3. With the broken ``.tf`` files out of the workdir, plan
     verification is re-run on the survivors -- previously
     ``blocked-by-sibling`` resources now pass cleanly without any
     cascade.

  4. Final WorkflowResult reports: imported (passed verification),
     needs_attention (quarantined), failed (couldn't even import).

Why both move-the-file AND state-rm:

  * Moving the .tf alone leaves an orphaned state entry. Next
    ``terraform plan`` would see "resource exists in state, no
    config -> destroy" -- the wrong default outcome.
  * Removing state alone leaves a buggy .tf in the workdir that
    will block the next plan again.
  * Doing both keeps state + workdir consistent at the cost of
    needing to re-import once the customer fixes the .tf in the
    quarantine dir (or hands it to the importer's writer for a
    fresh attempt).

Invocation gate:

The quarantine path is OPT-IN via the ``IMPORTER_AUTO_QUARANTINE``
environment variable (1/true/yes). When unset OR explicitly false,
the existing interactive HITL menu runs unchanged (back-compat for
the CLI operator team's local debugging flow). The Cloud Run /
SaaS path will set the env var unconditionally; locally an
operator can opt in for testing or stick with the menu.

In a future Phase 5 commit, the env var will be replaced by an
explicit ``run_workflow(headless=True)`` kwarg once
``run_workflow``'s call surface gets a refresh; the env var is the
minimum-disruption gate for landing CG-7 in Phase 4 hotfix scope.
"""

from __future__ import annotations

import os
import shutil
from typing import Optional

from common.logging import get_logger

from . import terraform_client

_log = get_logger(__name__)

QUARANTINE_DIRNAME = "_quarantine"


def is_auto_quarantine_enabled() -> bool:
    """True iff IMPORTER_AUTO_QUARANTINE is set to a truthy value.

    Recognised truthy values (case-insensitive): ``1``, ``true``,
    ``yes``, ``on``. Anything else -- including unset -- is False.

    Pure function; no I/O beyond reading the env var. Suitable for
    unit tests via ``unittest.mock.patch.dict(os.environ, ...)``.
    """
    raw = os.environ.get("IMPORTER_AUTO_QUARANTINE", "")
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def quarantine_path(workdir: str) -> str:
    """Return the absolute path to the workdir's quarantine subdir.

    Pure path computation; doesn't create the directory. Caller does
    that lazily on first quarantine event so we don't litter empty
    ``_quarantine/`` dirs in workdirs that never see a failure.
    """
    return os.path.join(workdir, QUARANTINE_DIRNAME)


def quarantine_resource(
    workdir: str,
    tf_address: str,
    hcl_filename: str,
    reason: str = "",
) -> bool:
    """Move a resource's .tf to ``_quarantine/`` AND remove from state.

    Both halves must succeed for the quarantine to be considered
    complete -- otherwise the workdir + state would diverge. On
    partial failure we attempt a best-effort revert (move the .tf
    back) and return False; caller decides whether to retry or
    surface the failure to the customer.

    Args:
        workdir: per-project workdir absolute path.
        tf_address: terraform resource address (e.g.
            ``google_cloud_run_v2_service.poc_cloudrun``). Used by
            ``terraform state rm``.
        hcl_filename: bare filename of the .tf file (e.g.
            ``google_cloud_run_v2_service_poc_cloudrun.tf``). Joined
            to ``workdir`` for the source path.
        reason: short human-readable note appended to the
            quarantine event log + dropped as a sibling
            ``<filename>.quarantine.txt`` so a future operator
            opening the quarantine dir sees WHY each file is there
            without git-archaeology.

    Returns:
        True iff both the file move AND the state remove succeeded.
        False on any failure (with best-effort partial-revert).
    """
    qdir = quarantine_path(workdir)
    src = os.path.join(workdir, hcl_filename)
    dst = os.path.join(qdir, hcl_filename)

    log = _log.bind(
        tf_address=tf_address, hcl_filename=hcl_filename, workdir=workdir,
    )

    if not os.path.isfile(src):
        log.warning(
            "quarantine_skip_missing_file",
            reason="source_file_does_not_exist",
        )
        return False

    # Lazy-create the quarantine directory on first event.
    try:
        os.makedirs(qdir, exist_ok=True)
    except OSError as e:
        log.error("quarantine_mkdir_failed", error=str(e))
        return False

    # Step 1: move the .tf file out of the workdir.
    try:
        shutil.move(src, dst)
    except (OSError, shutil.Error) as e:
        log.error("quarantine_move_failed", error=str(e))
        return False

    # Step 2: write the sibling reason file (operational hygiene --
    # makes the quarantine dir self-documenting).
    if reason:
        reason_path = dst + ".quarantine.txt"
        try:
            with open(reason_path, "w", encoding="utf-8") as f:
                f.write(
                    f"Quarantined: {tf_address}\n"
                    f"Source file: {hcl_filename}\n"
                    f"Reason:\n{reason}\n"
                )
        except OSError as e:
            # Reason file is informational; don't fail the whole
            # quarantine if it can't be written. Log + continue.
            log.warning("quarantine_reason_write_failed", error=str(e))

    # Step 3: remove the terraform state entry. If this fails, the
    # workdir would have a quarantined .tf BUT state would still
    # reference the resource -- next plan would see "resource in
    # state, no config -> destroy". Try to revert the file move on
    # state-rm failure to keep the two consistent.
    state_rm_ok = terraform_client.state_rm(tf_address, workdir=workdir)
    if not state_rm_ok:
        log.error("quarantine_state_rm_failed", reverting_file_move=True)
        try:
            shutil.move(dst, src)
        except (OSError, shutil.Error) as revert_err:
            # Best-effort revert failed too -- workdir + state are
            # now inconsistent. Surface as critical so an operator
            # can manually fix.
            log.critical(
                "quarantine_revert_failed",
                error=str(revert_err),
                manual_action_required=(
                    f"Move {dst} back to {src} OR re-import "
                    f"{tf_address} once .tf is fixed"
                ),
            )
        return False

    log.info(
        "quarantine_complete",
        quarantine_path=dst,
        reason=reason[:200] if reason else "",  # truncate for log volume
    )
    return True
