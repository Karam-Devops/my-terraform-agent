"""SRE result persistence — survive browser refresh + Cloud Run restarts.

Mirrors ``migrator/output/result_persistence.py`` exactly — same JSON
shape, same gzip + atomic-write pattern, same backend dispatch
(``file://`` today, ``gs://`` for Cloud Run multi-tenant). Differences:

  * Schema is the IncidentResult tree (alert + evidence + hypotheses
    + source_timings + bookkeeping), not the MigrationResult tree.
  * No per-run output directory — incidents aren't artifact-producing.
    Instead the destination is a directory keyed on
    ``<tenant>/<project>`` and each triage writes
    ``<alert_id>.json.gz`` inside it.
  * The registry tracks the *most recent triage* per user_key so a
    refreshed page can re-hydrate the last run automatically (the
    operator was almost certainly looking at it).

Why mirror migrator instead of share
------------------------------------
Two engines, two shapes. Generalising would mean templating the
serialize/deserialize over a TypeVar — significant boilerplate to
save one ~400-line file. Cheaper to keep them parallel and let each
evolve independently. The shared idea (backend-dispatch URL +
gzip + registry) is conceptual, not code.
"""

from __future__ import annotations

import dataclasses
import gzip
import json
import os
import re
import time
from typing import Any, Dict, Optional
from urllib.parse import urlparse

from common.logging import get_logger

from ..results import (
    AlertEnvelope,
    EvidenceItem,
    Hypothesis,
    IncidentResult,
    SourceTiming,
)


_log = get_logger(__name__)


# Bump on backwards-incompatible shape changes. Loader rejects mismatched
# versions rather than silently mis-loading — operator re-runs the triage.
SCHEMA_VERSION = 1

# File naming inside the destination directory.
# One file per triage so a busy project doesn't end up with a single
# enormous registry blob. Hidden prefix so operators browsing the
# bucket don't see them as noise.
_STATE_FILENAME_PATTERN = ".sre_triage_{alert_id}.json.gz"

# Per-user registry — tells the UI "the most recent triage for user_key
# X was alert_id Y at destination Z". Survives across page refreshes;
# pruned when the underlying file is gone.
_REGISTRY_ENV      = "SRE_REGISTRY_DIR"
_REGISTRY_FILENAME = "last_triages.json"
_DEFAULT_REGISTRY_DIRNAME = ".mtagent-sre"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def save_result(
    result: IncidentResult,
    *,
    destination: Optional[str] = None,
    user_key: str = "default",
) -> str:
    """Persist an IncidentResult so the UI can recover after a refresh.

    Args:
        result: the IncidentResult to snapshot.
        destination: storage-backend URL. Trailing slash optional.
            * None (default) → ``file://<registry_dir>/snapshots/``
            * ``file:///abs/path/`` → write under the given directory.
            * ``gs://bucket/prefix/`` → Cloud Storage.
        user_key: per-user registry slot. In multi-tenant Cloud Run
            this is typically ``<tenant>::<project>`` (matches the
            slot used by orchestrator's snapshot save).

    Returns:
        The fully-qualified URL of the saved blob, or empty string
        on persistence failure (best-effort; never raises).
    """
    if destination is None:
        destination = "file://" + os.path.join(
            _registry_dir(), "snapshots",
        )

    safe_alert_id = _sanitize_alert_id(result.alert.alert_id)
    if not safe_alert_id:
        _log.warning("persist_skipped_no_alert_id")
        return ""

    payload = _serialize(result)
    payload["_schema_version"] = SCHEMA_VERSION
    payload["_saved_at"] = time.time()
    payload["_user_key"] = user_key
    payload["_safe_alert_id"] = safe_alert_id

    try:
        filename = _STATE_FILENAME_PATTERN.format(alert_id=safe_alert_id)
        if destination.startswith("file://"):
            path = _save_file(destination, payload, filename=filename)
        elif destination.startswith("gs://"):
            path = _save_gs(destination, payload, filename=filename)
        else:
            _log.warning(
                "persist_backend_not_implemented",
                destination=destination,
                hint="Supported schemes: file://, gs://",
            )
            return ""

        _register_last_triage(
            user_key=user_key,
            destination=destination,
            alert_id=result.alert.alert_id,
            safe_alert_id=safe_alert_id,
        )
        _log.info(
            "persist_complete",
            destination=path,
            alert_id=result.alert.alert_id,
            evidence_count=len(result.evidence),
            hypothesis_count=len(result.hypotheses),
        )
        return path
    except Exception as e:  # noqa: BLE001 — best-effort
        _log.warning(
            "persist_failed",
            destination=destination,
            alert_id=result.alert.alert_id,
            error_type=type(e).__name__,
            error=str(e),
        )
        return ""


def load_result(
    *,
    destination: Optional[str] = None,
    alert_id: Optional[str] = None,
    user_key: str = "default",
) -> Optional[IncidentResult]:
    """Recover a previously persisted IncidentResult.

    Args:
        destination: backend URL of the directory. None → use the
            registry's most recent slot for ``user_key``.
        alert_id: which triage to load. None → use the registry's
            most recent slot for ``user_key``. Pair this with
            ``destination`` when reloading a specific past triage.
        user_key: per-user slot in the registry.

    Returns:
        Reconstituted IncidentResult, or None when no snapshot exists,
        the snapshot is corrupt, or the schema_version mismatches.
        Never raises — UI falls back to the empty state on None.
    """
    if destination is None or alert_id is None:
        entry = _lookup_last_triage(user_key=user_key)
        if not entry:
            return None
        destination = destination or entry.get("destination", "")
        alert_id = alert_id or entry.get("alert_id", "")

    safe_alert_id = _sanitize_alert_id(alert_id or "")
    if not safe_alert_id or not destination:
        return None

    filename = _STATE_FILENAME_PATTERN.format(alert_id=safe_alert_id)

    try:
        if destination.startswith("file://"):
            payload = _load_file(destination, filename=filename)
        elif destination.startswith("gs://"):
            payload = _load_gs(destination, filename=filename)
        else:
            _log.warning("load_backend_not_implemented", destination=destination)
            return None

        if payload is None:
            # Snapshot advertised but actually missing → prune the
            # stale registry slot so the UI doesn't keep offering an
            # impossible restore.
            _prune_registry_slot(user_key)
            return None

        version = payload.get("_schema_version")
        if version != SCHEMA_VERSION:
            _log.warning(
                "load_schema_mismatch",
                expected=SCHEMA_VERSION, found=version,
                hint="Re-run the triage to regenerate the snapshot.",
            )
            return None

        result = _deserialize(payload)
        _log.info(
            "load_complete",
            destination=destination,
            alert_id=alert_id,
            evidence_count=len(result.evidence),
            saved_at=payload.get("_saved_at"),
        )
        return result
    except Exception as e:  # noqa: BLE001 — best-effort
        _log.warning(
            "load_failed",
            destination=destination, alert_id=alert_id,
            error_type=type(e).__name__, error=str(e),
        )
        return None


def get_last_triage_info(*, user_key: str = "default") -> Optional[Dict[str, Any]]:
    """Peek at the registry — for surfacing a "Restore?" prompt.

    Returns dict with ``destination``, ``alert_id``, ``saved_at`` when
    a snapshot exists AND the underlying file is still reachable.
    Returns None when the slot is empty OR self-heals a stale entry.
    """
    registry = _read_registry()
    entry = registry.get(user_key)
    if not entry:
        return None

    destination = str(entry.get("destination", ""))
    safe_alert_id = str(entry.get("safe_alert_id", ""))
    if not destination or not safe_alert_id:
        _prune_registry_slot(user_key)
        return None

    filename = _STATE_FILENAME_PATTERN.format(alert_id=safe_alert_id)
    present = False
    if destination.startswith("file://"):
        dirpath = destination[len("file://"):]
        present = os.path.isfile(os.path.join(dirpath, filename))
    elif destination.startswith("gs://"):
        try:
            from google.cloud import storage  # type: ignore
            bucket_name, prefix = _parse_gs_url(destination)
            client = storage.Client()
            bucket = client.bucket(bucket_name)
            present = bucket.blob(f"{prefix}{filename}").exists(client)
        except Exception:  # noqa: BLE001 — best-effort
            present = False

    if not present:
        _log.info(
            "registry_entry_pruned_stale",
            user_key=user_key, destination=destination,
            reason="snapshot missing at advertised location",
        )
        _prune_registry_slot(user_key)
        return None

    return {
        "destination": destination,
        "alert_id":    entry.get("alert_id", ""),
        "saved_at":    entry.get("saved_at", 0),
    }


# ---------------------------------------------------------------------------
# Backend: file://
# ---------------------------------------------------------------------------


def _save_file(destination: str, payload: Dict[str, Any], *, filename: str) -> str:
    """file:// → write gzipped JSON to <dir>/<filename>.

    Atomic via .tmp + os.replace so a crash mid-write can't poison
    a future load.
    """
    dirpath = destination[len("file://"):]
    os.makedirs(dirpath, exist_ok=True)
    path = os.path.join(dirpath, filename)
    tmp = path + ".tmp"
    raw = json.dumps(payload, sort_keys=True, default=_json_default).encode("utf-8")
    with gzip.open(tmp, "wb", compresslevel=6) as f:
        f.write(raw)
    os.replace(tmp, path)
    return path


def _load_file(destination: str, *, filename: str) -> Optional[Dict[str, Any]]:
    dirpath = destination[len("file://"):]
    path = os.path.join(dirpath, filename)
    if not os.path.isfile(path):
        return None
    with gzip.open(path, "rb") as f:
        return json.loads(f.read().decode("utf-8"))


# ---------------------------------------------------------------------------
# Backend: gs:// (Cloud Run multi-tenant)
# ---------------------------------------------------------------------------


def _parse_gs_url(url: str) -> "tuple[str, str]":
    """``gs://bucket/some/prefix/`` → (bucket, prefix_with_trailing_slash)."""
    parsed = urlparse(url)
    bucket = parsed.netloc
    prefix = parsed.path.lstrip("/")
    if prefix and not prefix.endswith("/"):
        prefix += "/"
    return bucket, prefix


def _save_gs(destination: str, payload: Dict[str, Any], *, filename: str) -> str:
    try:
        from google.cloud import storage  # type: ignore
    except ImportError as e:
        raise RuntimeError(
            "gs:// backend requested but google-cloud-storage is not "
            "installed. Add it to requirements.txt and rebuild."
        ) from e

    bucket_name, prefix = _parse_gs_url(destination)
    blob_name = f"{prefix}{filename}"
    raw = json.dumps(payload, sort_keys=True, default=_json_default).encode("utf-8")
    gzipped = gzip.compress(raw, compresslevel=6)

    client = storage.Client()
    bucket = client.bucket(bucket_name)
    blob = bucket.blob(blob_name)
    blob.content_encoding = "gzip"
    blob.upload_from_string(gzipped, content_type="application/json")
    return f"gs://{bucket_name}/{blob_name}"


def _load_gs(destination: str, *, filename: str) -> Optional[Dict[str, Any]]:
    try:
        from google.cloud import storage  # type: ignore
    except ImportError:
        return None

    bucket_name, prefix = _parse_gs_url(destination)
    client = storage.Client()
    bucket = client.bucket(bucket_name)
    blob = bucket.blob(f"{prefix}{filename}")
    if not blob.exists(client):
        return None
    raw = blob.download_as_bytes()
    raw = gzip.decompress(raw)
    return json.loads(raw.decode("utf-8"))


# ---------------------------------------------------------------------------
# Per-user registry (last_triages.json)
# ---------------------------------------------------------------------------


def _registry_dir() -> str:
    """Where the registry + default snapshots dir live."""
    override = os.environ.get(_REGISTRY_ENV)
    if override:
        return override
    return os.path.join(os.path.expanduser("~"), _DEFAULT_REGISTRY_DIRNAME)


def _read_registry() -> Dict[str, Any]:
    path = os.path.join(_registry_dir(), _REGISTRY_FILENAME)
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
            if isinstance(data, dict):
                return data
    except (OSError, json.JSONDecodeError):
        # Corrupt registry → treat as empty; next save overwrites.
        pass
    return {}


def _register_last_triage(
    *,
    user_key: str,
    destination: str,
    alert_id: str,
    safe_alert_id: str,
) -> None:
    try:
        os.makedirs(_registry_dir(), exist_ok=True)
        registry = _read_registry()
        registry[user_key] = {
            "destination":   destination,
            "alert_id":      alert_id,
            "safe_alert_id": safe_alert_id,
            "saved_at":      time.time(),
        }
        path = os.path.join(_registry_dir(), _REGISTRY_FILENAME)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(registry, f, indent=2, sort_keys=True)
        os.replace(tmp, path)
    except OSError as e:
        _log.warning("registry_write_failed", user_key=user_key, error=str(e))


def _lookup_last_triage(*, user_key: str) -> Optional[Dict[str, Any]]:
    return _read_registry().get(user_key)


def _prune_registry_slot(user_key: str) -> None:
    try:
        registry = _read_registry()
        if user_key not in registry:
            return
        del registry[user_key]
        path = os.path.join(_registry_dir(), _REGISTRY_FILENAME)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(registry, f, indent=2, sort_keys=True)
        os.replace(tmp, path)
    except OSError as e:
        _log.warning("registry_prune_failed", user_key=user_key, error=str(e))


# ---------------------------------------------------------------------------
# (de)serialization
# ---------------------------------------------------------------------------


def _serialize(result: IncidentResult) -> Dict[str, Any]:
    """IncidentResult → JSON-friendly dict.

    ``dataclasses.asdict`` works cleanly here because IncidentResult's
    fields are all dataclasses (AlertEnvelope, EvidenceItem, Hypothesis,
    SourceTiming) or plain types — no custom containers. The Dict[str,
    Any] raw_payload fields round-trip naturally as long as their
    contents are JSON-serializable (they come from Pub/Sub / audit
    logs, both of which are JSON-native, so this holds in practice).
    """
    return {
        "alert":          dataclasses.asdict(result.alert),
        "project_id":     result.project_id,
        "tenant_id":      result.tenant_id,
        "lookback_min":   result.lookback_min,
        "evidence":       [dataclasses.asdict(e) for e in result.evidence],
        "hypotheses":     [dataclasses.asdict(h) for h in result.hypotheses],
        "source_timings": [dataclasses.asdict(s) for s in result.source_timings],
        "started_at":     result.started_at,
        "completed_at":   result.completed_at,
        "duration_s":     result.duration_s,
        "notes":          list(result.notes),
        "errors":         list(result.errors),
    }


def _deserialize(payload: Dict[str, Any]) -> IncidentResult:
    """JSON dict → IncidentResult.

    Forward-compat: unknown top-level keys are ignored. Missing keys
    get the dataclass default — covers older snapshots that pre-date
    new fields. Same pattern migrator uses.
    """
    alert_d = payload.get("alert") or {}
    alert = AlertEnvelope(
        alert_id=alert_d.get("alert_id", ""),
        source=alert_d.get("source", ""),
        fired_at=alert_d.get("fired_at", ""),
        policy_name=alert_d.get("policy_name", ""),
        summary=alert_d.get("summary", ""),
        severity=alert_d.get("severity", "SEV2"),
        resource_refs=list(alert_d.get("resource_refs", [])),
        project_id=alert_d.get("project_id"),
        labels=dict(alert_d.get("labels", {})),
        raw_payload=dict(alert_d.get("raw_payload", {})),
        pubsub_message_id=alert_d.get("pubsub_message_id"),
        pubsub_ack_id=alert_d.get("pubsub_ack_id"),
    )

    evidence = [
        EvidenceItem(
            evidence_id=e.get("evidence_id", ""),
            source=e.get("source", ""),
            timestamp=e.get("timestamp", ""),
            change_type=e.get("change_type", ""),
            resource_ref=e.get("resource_ref", ""),
            actor=e.get("actor", ""),
            summary=e.get("summary", ""),
            related_refs=list(e.get("related_refs", [])),
            relevance_score=float(e.get("relevance_score", 0.0)),
            raw_payload=dict(e.get("raw_payload", {})),
        )
        for e in payload.get("evidence", [])
    ]

    hypotheses = [
        Hypothesis(
            rank=int(h.get("rank", 0)),
            confidence=h.get("confidence", "LOW"),
            confidence_pct=int(h.get("confidence_pct", 0)),
            headline=h.get("headline", ""),
            reasoning=list(h.get("reasoning", [])),
            cited_evidence=list(h.get("cited_evidence", [])),
            recommended_actions=list(h.get("recommended_actions", [])),
        )
        for h in payload.get("hypotheses", [])
    ]

    source_timings = [
        SourceTiming(
            source=s.get("source", ""),
            item_count=int(s.get("item_count", 0)),
            duration_ms=int(s.get("duration_ms", 0)),
            status=s.get("status", "ok"),
            error=s.get("error"),
        )
        for s in payload.get("source_timings", [])
    ]

    return IncidentResult(
        alert=alert,
        project_id=payload.get("project_id", ""),
        tenant_id=payload.get("tenant_id", "default"),
        lookback_min=int(payload.get("lookback_min", 60)),
        evidence=evidence,
        hypotheses=hypotheses,
        source_timings=source_timings,
        started_at=payload.get("started_at", ""),
        completed_at=payload.get("completed_at", ""),
        duration_s=float(payload.get("duration_s", 0.0)),
        notes=list(payload.get("notes", [])),
        errors=list(payload.get("errors", [])),
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _sanitize_alert_id(alert_id: str) -> str:
    """Filesystem-safe slug from an alert_id.

    Cloud Monitoring incident_ids contain dots + slashes that aren't
    portable across filesystems / GCS object names. Same character
    set as migrator's persistence — keep only [A-Za-z0-9_.-].
    """
    if not alert_id:
        return ""
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "_", alert_id.strip()).strip("_")
    return cleaned[:200]  # hard cap so a pathologically long ID can't blow up paths


def _json_default(obj: Any) -> Any:
    """Fallback for non-JSON-serializable values (datetimes, sets, etc.)."""
    if hasattr(obj, "isoformat"):
        return obj.isoformat()
    if isinstance(obj, (set, frozenset)):
        return sorted(obj)
    return str(obj)
