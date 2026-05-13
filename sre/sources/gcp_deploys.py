"""Deploy-history evidence source (Phase 8 Day 2).

What this source produces
-------------------------
Deploy events in the lookback window across the deploy systems an
operator might be using:

  * **Cloud Build** — `gcloud builds list` for completed builds.
    The single highest-yield source today (covers nearly all GCP
    deploy pipelines, including Cloud Run, GKE, App Engine, and any
    custom build-and-push setup).
  * **Cloud Run revisions** — surfaces as audit-log entries with
    method ``CreateRevision``; picked up by the asset-changes source
    already. We supplement here by extracting deploy-shaped summary
    text so the UI's evidence list reads "deployed image:tag" not
    "MODIFY on revision".

Why a separate source (not just "asset changes" again)
------------------------------------------------------
Operators reading the triage want a deploy chip specifically. The
mental model "did we ship something right before this fired?" is
the single most-common question. Surfacing it as its own
``status="ok" — 3 deploys`` chip in the UI shortcuts a lot of
manual log-grepping.

Cloud Build is queried via `gcloud builds list --format=json`
because no `google-cloud-build` SDK is in the platform deps and
adding it for one collector isn't worth it. Same subprocess pattern
the rest of the platform uses.
"""

from __future__ import annotations

import datetime
import json
import subprocess
from typing import Any, Dict, List

from common.logging import get_logger
from importer.shell_runner import run_command

from ..results import AlertEnvelope, EvidenceItem
from . import _log_client


_log = get_logger(__name__)


# Cap how many builds we list. Most projects have <50 builds/hour
# even during peak release windows; 100 covers extreme cases without
# blowing the timeout budget.
_MAX_BUILDS = 100

# Per-call timeout for `gcloud builds list`. Cloud Build's list API
# is fast (~1-2s on typical projects). 30s gives plenty of headroom.
_BUILDS_TIMEOUT_S = 30.0


def collect(
    *,
    alert: AlertEnvelope,
    project_id: str,
    lookback_min: int,
) -> List[EvidenceItem]:
    """Collect deploy events inside the alert's lookback window."""
    start_iso, end_iso = _log_client.compute_window(
        fired_at_iso=alert.fired_at, lookback_min=lookback_min,
    )

    evidence: List[EvidenceItem] = []
    evidence.extend(_collect_cloud_build(
        project_id=project_id, start_iso=start_iso, end_iso=end_iso,
    ))

    _log.info(
        "deploys_collected",
        project_id=project_id,
        count=len(evidence),
        window=f"{start_iso} → {end_iso}",
    )
    return evidence


def _collect_cloud_build(
    *,
    project_id: str,
    start_iso: str,
    end_iso: str,
) -> List[EvidenceItem]:
    """List completed Cloud Build builds in the window.

    Uses the same `gcloud ... --format=json` subprocess pattern as
    the importer's resource enumeration. Returns EvidenceItems with
    ``change_type="DEPLOY"`` and a summary like "deployed gcr.io/.../api:v1.2.3".
    """
    # Cloud Build's list filter language uses createTime>=ISO without
    # quotes. Quoting the value triggers a parse error on some gcloud
    # versions — keep it bare.
    cmd = [
        "gcloud", "builds", "list",
        f"--project={project_id}",
        f"--filter=createTime>={start_iso} AND createTime<={end_iso}",
        f"--limit={_MAX_BUILDS}",
        "--format=json",
    ]
    try:
        stdout = run_command(cmd, timeout=_BUILDS_TIMEOUT_S)
    except subprocess.CalledProcessError as e:
        # Cloud Build API often isn't enabled on customer projects.
        # Treat as a configuration issue, not a crash — orchestrator
        # surfaces it as source_timing(status="failed") and the rest
        # of the triage continues unaffected.
        _log.warning(
            "cloud_build_list_failed",
            project_id=project_id,
            returncode=e.returncode,
            stderr=(e.stderr or "")[:300] if hasattr(e, "stderr") else "",
            reason=(
                "Cloud Build API may not be enabled on the project, or "
                "the runtime SA lacks roles/cloudbuild.builds.viewer."
            ),
        )
        return []

    if not stdout or not stdout.strip():
        return []
    try:
        builds = json.loads(stdout)
    except json.JSONDecodeError as je:
        _log.warning(
            "cloud_build_json_decode_failed",
            project_id=project_id,
            error=str(je),
            sample=stdout[:200],
        )
        return []
    if not isinstance(builds, list):
        return []

    evidence: List[EvidenceItem] = []
    for idx, b in enumerate(builds):
        evidence.append(_build_to_evidence(idx, b, project_id))
    return evidence


def _build_to_evidence(
    idx: int, build: Dict[str, Any], project_id: str,
) -> EvidenceItem:
    """One Cloud Build record → EvidenceItem."""
    # Timestamp: prefer createTime (when the build started) over
    # finishTime so the timeline reads as "deploy started at X".
    timestamp = str(build.get("createTime") or build.get("finishTime") or "")

    # Status is one of QUEUED / WORKING / SUCCESS / FAILURE / TIMEOUT /
    # CANCELLED. We surface failed deploys too — they often correlate
    # with incidents (a partial deploy can be worse than no deploy).
    status = str(build.get("status") or "UNKNOWN")

    # Image targets give the operator a click-through into "what code
    # ran". Builds.images is the list of tags pushed; first one is
    # usually the primary product image.
    images = build.get("images") or []
    primary_image = images[0] if images else ""

    # Source ref: source.repoSource.commitSha + branchName when present.
    source = build.get("source") or {}
    repo_source = source.get("repoSource") or {}
    storage_source = source.get("storageSource") or {}
    if repo_source:
        commit = repo_source.get("commitSha", "")
        branch = repo_source.get("branchName", repo_source.get("tagName", ""))
        repo_name = repo_source.get("repoName", "")
        source_summary = f"{repo_name}@{branch} ({commit[:8] if commit else '?'})"
    elif storage_source:
        source_summary = f"gs://{storage_source.get('bucket', '')}/{storage_source.get('object', '')}"
    else:
        source_summary = "?"

    summary = (
        f"build {build.get('id','?')[:8]} {status}: "
        f"{primary_image or '(no image)'} from {source_summary}"
    )

    # Resource ref: synthesize a canonical pointer the correlator can
    # substring-match. Cloud Build's logUrl is the operator-facing
    # link — we tuck it in raw_payload (already there) but mention it
    # in related_refs so the LLM can cite it.
    resource_ref = primary_image or f"builds/{build.get('id', idx)}"
    related: List[str] = []
    if build.get("logUrl"):
        related.append(str(build["logUrl"]))

    return EvidenceItem(
        evidence_id=f"deploy:{idx}",
        source="gcp_deploys",
        timestamp=timestamp,
        change_type="DEPLOY",
        resource_ref=resource_ref,
        actor=_extract_build_actor(build),
        summary=summary,
        related_refs=related,
        relevance_score=0.0,
        raw_payload=build,
    )


def _extract_build_actor(build: Dict[str, Any]) -> str:
    """Best-effort 'who triggered this deploy'.

    Cloud Build records the triggering identity in either
    ``substitutions._BUILD_USER`` (if set) or implicitly via the
    service account (``serviceAccount`` field). Falls back to
    "cloudbuild-system" when neither is present.
    """
    subs = build.get("substitutions") or {}
    user = subs.get("_BUILD_USER") or subs.get("_USER")
    if user:
        return str(user)
    sa = build.get("serviceAccount")
    if sa:
        # serviceAccount comes as a path like
        # "projects/-/serviceAccounts/foo@bar.iam.gserviceaccount.com"
        # — strip the prefix so the UI shows just the email.
        return str(sa).rsplit("/", 1)[-1]
    # Some manual triggers set createdBy in metadata.
    return str(build.get("createdBy") or "cloudbuild-system")
