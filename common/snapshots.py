# common/snapshots.py
"""Engine-result snapshot persistence to GCS (PSA-9).

After each engine run (importer / translator / detector / policy),
we serialize the result dict as JSON and write two GCS objects:

  * ``snapshots/<engine>/latest.json`` -- single-object lookup for
    the Dashboard's "current state" view (no listing required).
  * ``snapshots/<engine>/history/<iso-timestamp>.json`` -- audit
    trail. Customer can browse "what did the drift report look like
    last Tuesday?" without us keeping engine logs forever.

GCS layout (under the same per-tenant + per-project prefix used by
PSA-3's hydrate/persist):

    gs://<bucket>/tenants/<tenant>/projects/<project>/snapshots/
      importer/latest.json
      importer/history/2026-04-28T15-30-22Z.json
      translator/latest.json
      translator/history/...
      detector/latest.json
      detector/history/...
      policy/latest.json
      policy/history/...

The Dashboard (Phase 6 PUI-2) reads ``latest.json`` for each engine
to populate the hero metrics + recent-activity feed without
re-running the engine on every page load (per CG-8H spec, this is
the "cached snapshot" pattern that makes the inventory page cheap).

Gating:

  * Same gate as PSA-5's GCS backend wiring: the
    ``MTAGENT_PERSIST_SNAPSHOTS`` env var (default ON when set; OFF
    when unset for local-dev backward compat).
  * Cloud Run cloudbuild.yaml sets it to "1".
  * Local-dev runs without the env var see write_snapshot as a
    no-op -- engines run identically to today.

Failure handling:

  * Snapshot writes are NEVER fatal to the engine workflow. Caller
    wraps in try/except and logs warnings. Customer sees the engine
    run completed even if Dashboard data is stale.
  * Reads return None on missing / unreadable -- caller handles
    "no data yet" gracefully (Dashboard renders an empty-state hint).

Why subprocess gcloud (not google-cloud-storage SDK):

  * Matches PSA-3's pattern (no new dependency, same auth chain)
  * Snapshots are small (~1-5 KB each) so subprocess overhead is
    negligible vs the JSON serialization
  * One less Python lib to keep version-pinned in requirements.txt
"""

from __future__ import annotations

import datetime
import json
import os
import re
import subprocess
import tempfile
from typing import Optional

from common.logging import get_logger

_log = get_logger(__name__)


# Recognised engine names. Centralised constant so a typo at a call
# site fails the validation check rather than silently writing to a
# misspelled GCS path.
_VALID_ENGINES = ("importer", "translator", "detector", "policy")


# Same regexes as common/storage.py for path-traversal safety.
_TENANT_ID_RE = re.compile(r"^[a-zA-Z0-9][-a-zA-Z0-9_]{0,62}$")
_PROJECT_ID_RE = re.compile(r"^[a-z][-a-z0-9]{4,28}[a-z0-9]$")


def _bucket() -> str:
    """Return the snapshot bucket name (same source of truth as PSA-3)."""
    return os.environ.get("MTAGENT_STATE_BUCKET", "mtagent-state-dev")


def snapshots_enabled() -> bool:
    """True iff snapshot persistence should fire on engine completion.

    Gated on ``MTAGENT_PERSIST_SNAPSHOTS`` env var. Default OFF
    preserves local-dev behaviour (engines run + log results as
    today; no GCS writes attempted, no auth required).

    Cloud Run sets the env to ``"1"`` via cloudbuild.yaml.
    """
    raw = os.environ.get("MTAGENT_PERSIST_SNAPSHOTS", "").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def _validate_engine(engine_name: str) -> None:
    if engine_name not in _VALID_ENGINES:
        raise ValueError(
            f"Invalid engine_name {engine_name!r}: must be one of "
            f"{_VALID_ENGINES}"
        )


def _validate_ids(tenant_id: str, project_id: str) -> None:
    """Path-traversal guard, same shape as common/storage.py."""
    if not _TENANT_ID_RE.match(tenant_id):
        raise ValueError(f"Invalid tenant_id {tenant_id!r}")
    if not _PROJECT_ID_RE.match(project_id):
        raise ValueError(f"Invalid project_id {project_id!r}")


def _snapshot_prefix(tenant_id: str, project_id: str, engine_name: str) -> str:
    """Return the gs:// URI prefix for a (tenant, project, engine).

    Trailing slash on prefix; caller appends ``latest.json`` or
    ``history/<ts>.json``.
    """
    bucket = _bucket()
    return (
        f"gs://{bucket}/tenants/{tenant_id}/projects/{project_id}"
        f"/snapshots/{engine_name}/"
    )


def _utc_timestamp() -> str:
    """Return an ISO-8601 timestamp safe for use as a GCS object name.

    Uses ``-`` instead of ``:`` (GCS allows both, but ``:`` confuses
    operators using gsutil/gcloud + it's not URL-safe).
    """
    now = datetime.datetime.now(datetime.timezone.utc)
    return now.strftime("%Y-%m-%dT%H-%M-%SZ")


def _run_gcloud(args: list) -> subprocess.CompletedProcess:
    """Run a gcloud command, raising CalledProcessError on non-zero.

    Pulled into a helper so tests can patch a single seam:
    ``patch("common.snapshots._run_gcloud")``.
    """
    return subprocess.run(
        args, check=True, capture_output=True, text=True,
    )


def write_snapshot(
    engine_name: str,
    result: dict,
    project_id: str,
    *,
    tenant_id: Optional[str] = None,
) -> bool:
    """Persist an engine result dict as JSON to GCS.

    Writes TWO objects:
      * ``snapshots/<engine>/latest.json``  (overwritten each run)
      * ``snapshots/<engine>/history/<iso-timestamp>.json``  (immutable)

    Both are uploaded via ``gcloud storage cp``. If the env-var gate
    (``MTAGENT_PERSIST_SNAPSHOTS``) is unset, this is a no-op.

    Args:
        engine_name: One of {"importer", "translator", "detector",
            "policy"}. Other values raise ValueError.
        result: JSON-serializable dict. For the 3 engines with
            ``as_fields()`` methods, pass that. For policy, pass an
            inline summary dict.
        project_id: GCP project ID being scanned.
        tenant_id: Multi-tenant identifier (defaults to "default").

    Returns:
        ``True`` if writes succeeded; ``False`` if the env-var gate
        was off (no writes attempted).

    Raises:
        ValueError: engine_name / project_id / tenant_id failed
            validation (only when env is enabled and we'd actually
            write).
        TypeError: ``result`` is not JSON-serializable.
        subprocess.CalledProcessError: gcloud upload failed
            (network, perms). Caller MAY swallow + log if snapshot
            persistence is best-effort.
    """
    if not snapshots_enabled():
        return False

    _validate_engine(engine_name)
    tenant = tenant_id or "default"
    _validate_ids(tenant, project_id)

    payload = json.dumps(result, indent=2, sort_keys=True, default=str)
    prefix = _snapshot_prefix(tenant, project_id, engine_name)
    timestamp = _utc_timestamp()

    _log.info(
        "snapshot_write_start",
        engine=engine_name,
        tenant_id=tenant,
        project_id=project_id,
        prefix=prefix,
        timestamp=timestamp,
        payload_bytes=len(payload),
    )

    # Use a tempfile -> gcloud cp pattern. gcloud storage cp can
    # accept stdin via "-" but tempfile is more portable + easier
    # to debug failed uploads.
    tmp_fd, tmp_path = tempfile.mkstemp(suffix=".json", prefix="mtagent-snap-")
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as fh:
            fh.write(payload)

        # Write history first (immutable; the audit-trail copy).
        history_uri = f"{prefix}history/{timestamp}.json"
        _run_gcloud(["gcloud", "storage", "cp", tmp_path, history_uri])

        # Then overwrite latest.json (the Dashboard's read target).
        # Doing history first means a Dashboard read mid-write sees
        # the previous-good latest until the new one lands.
        latest_uri = f"{prefix}latest.json"
        _run_gcloud(["gcloud", "storage", "cp", tmp_path, latest_uri])
    finally:
        try:
            os.remove(tmp_path)
        except OSError:
            pass

    _log.info(
        "snapshot_write_complete",
        engine=engine_name,
        tenant_id=tenant,
        project_id=project_id,
        timestamp=timestamp,
    )
    return True


def read_latest_snapshot(
    engine_name: str,
    project_id: str,
    *,
    tenant_id: Optional[str] = None,
) -> Optional[dict]:
    """Read the latest snapshot for an engine. Returns None if absent.

    Used by the Dashboard (Phase 6 PUI-2) to populate metric cards
    without re-running the engine. Always tolerant of "no data yet"
    -- returns None, caller renders empty-state.

    Args:
        engine_name: One of the recognised engines.
        project_id: GCP project ID.
        tenant_id: Multi-tenant identifier (defaults to "default").

    Returns:
        The decoded dict, or ``None`` if the snapshot doesn't exist
        OR cannot be read (network, perms, malformed JSON).

    Raises:
        ValueError: engine_name / IDs failed validation.

    Notes:
        Does NOT distinguish between "no data" (engine never ran)
        and "couldn't read" (transient network failure). Both
        return None. Caller can re-call later if it cares about
        the difference -- a real "missing" is permanent until the
        next engine run, while "transient" recovers on retry.
    """
    _validate_engine(engine_name)
    tenant = tenant_id or "default"
    _validate_ids(tenant, project_id)

    prefix = _snapshot_prefix(tenant, project_id, engine_name)
    latest_uri = f"{prefix}latest.json"

    tmp_fd, tmp_path = tempfile.mkstemp(suffix=".json", prefix="mtagent-snap-r-")
    os.close(tmp_fd)
    try:
        try:
            _run_gcloud(["gcloud", "storage", "cp", latest_uri, tmp_path])
        except subprocess.CalledProcessError as e:
            # Most likely "object not found" -- engine hasn't run yet.
            # Could also be perms / network. Either way, return None
            # and let the caller render empty-state.
            _log.info(
                "snapshot_read_missing",
                engine=engine_name,
                tenant_id=tenant,
                project_id=project_id,
                latest_uri=latest_uri,
                stderr=(e.stderr or "")[:200],
            )
            return None

        try:
            with open(tmp_path, "r", encoding="utf-8") as fh:
                return json.load(fh)
        except (OSError, json.JSONDecodeError) as e:
            _log.warning(
                "snapshot_read_malformed",
                engine=engine_name,
                tenant_id=tenant,
                project_id=project_id,
                error=str(e),
            )
            return None
    finally:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
