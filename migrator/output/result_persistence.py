"""Migrator result persistence — survive browser refresh + Cloud Run restarts.

Problem we solve
================
Streamlit's ``st.session_state`` is per-WebSocket connection. The moment
the browser refreshes (F5, accidental tab close, OS sleep), the
connection is replaced and the operator's freshly computed
MigrationResult is gone — they have to re-run the engine. On Cloud Run
the same thing happens when a container recycles or when subsequent
requests land on a different replica.

This module makes results durable by serializing them to JSON next to
the emitted Terraform tree (so they sit with the rest of the artifacts
the operator cares about) plus a small per-user "last output_dir"
registry under ``~/.migrator/`` that lets the UI rediscover the most
recent run on page load.

Storage-backend agnostic
========================
``save_result()`` / ``load_result()`` take a ``destination`` string in
URL-like form. Today we only implement ``file://`` (local FS). The
Cloud Run rollout swaps in ``gs://<bucket>/<tenant>/`` or
``s3://<bucket>/<tenant>/`` without touching callers. The dispatch
happens inside the two top-level functions; new backends register by
adding a branch and a pair of helpers.

JSON shape
==========
The on-disk blob is what the UI needs to render the Results stage —
NOT the full dataclass tree. Resources, confidence findings, dep edges
all flatten to plain dicts. ``DiscoveredResource`` / ``ConfidenceFinding``
/ ``DependencyEdge`` round-trip via their dataclass fields.

The format is intentionally NOT a pickle — pickles break across Python
versions and across our own dataclass-shape changes. JSON survives both
and is human-debuggable, which matters when an operator emails us a
broken run.

A schema_version field guards forward-compat: when we add new
MigrationResult fields, ``load_result()`` populates them with defaults
rather than crashing on old snapshots.
"""

from __future__ import annotations

import dataclasses
import gzip
import json
import os
import time
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

from common.logging import get_logger
from migrator.results import (
    ConfidenceFinding,
    DependencyEdge,
    DiscoveredResource,
    MigrationResult,
)


_log = get_logger(__name__)

# Bump this when the on-disk shape changes in a backwards-incompatible
# way. Older snapshots get rejected with a clear "re-run the migration"
# message rather than silently mis-loading.
SCHEMA_VERSION = 1

# Filenames written under ``<output_dir>/``. Hidden (leading dot) so
# they don't pollute the operator's view of the artifacts they care
# about. The .gz variant is preferred (smaller — 246 KB → ~30 KB on the
# simple fixture; ~5x reduction on the 941-resource customer); the
# plain .json fallback is for backwards-compat with snapshots written
# before compression landed.
STATE_FILENAME_GZ   = ".migrator_state.json.gz"
STATE_FILENAME      = ".migrator_state.json"   # legacy / human-readable fallback

# Per-user registry: tells the UI where the most recent migration's
# output_dir lives, so a hard browser refresh can rediscover it.
# Lives under ``~/.migrator/`` (or the override env var in tests).
_REGISTRY_ENV = "MIGRATOR_REGISTRY_DIR"
_REGISTRY_FILENAME = "last_runs.json"


# ---------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------


def save_result(
    result: MigrationResult,
    *,
    destination: Optional[str] = None,
    user_key: str = "default",
) -> str:
    """Persist a MigrationResult so the UI can recover it after a refresh.

    Args:
        result: the MigrationResult to snapshot.
        destination: storage-backend URL.
            * None (default) → ``file://<result.output_dir>``.
            * ``file:///abs/path/`` → write to the given directory.
            * ``gs://bucket/prefix/`` → (future) Cloud Storage.
            * ``s3://bucket/prefix/`` → (future) S3.
        user_key: per-user / per-tenant slot in the "last runs"
            registry. Lets one Cloud Run replica serve multiple
            operators without crossing wires. In single-user local
            mode this stays "default".

    Returns:
        The fully-qualified URL where the state blob now lives. Useful
        for logging and for round-tripping in tests.

    Best-effort: this function never raises. If persistence fails
    (disk full, permission denied, network glitch on gs://), it logs a
    warning and returns an empty string. The engine result is unaffected
    — the operator just won't be able to recover from a refresh until
    the underlying issue is resolved.
    """
    if destination is None:
        if not result.output_dir:
            _log.warning("persist_skipped_no_output_dir")
            return ""
        destination = f"file://{result.output_dir}"

    payload = _serialize(result)
    payload["_schema_version"] = SCHEMA_VERSION
    payload["_saved_at"] = time.time()
    payload["_user_key"] = user_key

    try:
        if destination.startswith("file://"):
            path = _save_file(destination, payload)
        elif destination.startswith("gs://"):
            path = _save_gs(destination, payload)
        else:
            _log.warning(
                "persist_backend_not_implemented",
                destination=destination,
                hint="Supported schemes: file://, gs://. s3:// not yet implemented.",
            )
            return ""

        _register_last_run(user_key=user_key, output_dir=result.output_dir or "",
                           destination=destination)
        _log.info("persist_complete", destination=destination,
                  resource_count=len(result.resources))
        return path

    except Exception as e:  # noqa: BLE001 -- best-effort persistence
        _log.warning(
            "persist_failed",
            destination=destination,
            error_type=type(e).__name__,
            error=str(e),
        )
        return ""


def load_result(
    destination: Optional[str] = None,
    *,
    user_key: str = "default",
) -> Optional[MigrationResult]:
    """Recover a previously persisted MigrationResult.

    Args:
        destination: where to load from. Same scheme as ``save_result``.
            None → ask the registry for the most recent run keyed by
            ``user_key`` and load from there. This is the path the UI
            takes on page-load after a browser refresh.
        user_key: per-user slot in the registry.

    Returns:
        A reconstituted MigrationResult, or None when:
        * no run has been persisted for this user_key yet, or
        * the snapshot is from an incompatible schema_version, or
        * the on-disk JSON is corrupt.

    Like ``save_result``, this never raises — the UI gracefully falls
    back to the empty state when load returns None.
    """
    if destination is None:
        destination = _lookup_last_run(user_key=user_key)
        if not destination:
            return None

    try:
        if destination.startswith("file://"):
            payload = _load_file(destination)
        elif destination.startswith("gs://"):
            payload = _load_gs(destination)
        else:
            _log.warning("load_backend_not_implemented", destination=destination)
            return None

        if payload is None:
            # State file was advertised but isn't actually there — most
            # likely operator deleted the output dir between get_last_run_info
            # and the restore click. Prune the slot so subsequent page
            # loads don't keep offering an impossible restore.
            _prune_registry_slot(user_key)
            return None

        version = payload.get("_schema_version")
        if version != SCHEMA_VERSION:
            _log.warning(
                "load_schema_mismatch",
                expected=SCHEMA_VERSION,
                found=version,
                hint="Re-run the migration to regenerate the snapshot.",
            )
            return None

        result = _deserialize(payload)
        _log.info(
            "load_complete",
            destination=destination,
            resource_count=len(result.resources),
            saved_at=payload.get("_saved_at"),
        )
        return result

    except Exception as e:  # noqa: BLE001 -- best-effort
        _log.warning(
            "load_failed",
            destination=destination,
            error_type=type(e).__name__,
            error=str(e),
        )
        return None


def get_last_run_info(*, user_key: str = "default") -> Optional[Dict[str, Any]]:
    """Read-only peek at the registry — for surfacing a "Restore?" prompt.

    Returns a dict with ``destination``, ``output_dir``, ``saved_at``
    when a run exists AND the underlying state file is still on disk.
    Returns None when the slot is empty OR the file is gone (e.g.,
    operator deleted the output dir manually, or the temp volume got
    recycled on Cloud Run). The UI uses this to decide whether to
    show the restore button BEFORE actually loading the (potentially
    large) snapshot.

    Self-healing: if the registry points at a missing destination, the
    stale slot is auto-pruned so the banner doesn't keep advertising a
    recovery that can't succeed.
    """
    registry = _read_registry()
    entry = registry.get(user_key)
    if not entry:
        return None

    destination = str(entry.get("destination", ""))
    # Verify the snapshot actually exists where the registry claims.
    # Check both compressed and legacy filenames to handle the
    # transition window post-compression-landing.
    snapshot_present = False
    if destination.startswith("file://"):
        dirpath = destination[len("file://"):]
        snapshot_present = (
            os.path.isfile(os.path.join(dirpath, STATE_FILENAME_GZ))
            or os.path.isfile(os.path.join(dirpath, STATE_FILENAME))
        )
    elif destination.startswith("gs://"):
        try:
            from google.cloud import storage  # type: ignore
            bucket_name, prefix = _parse_gs_url(destination)
            client = storage.Client()
            bucket = client.bucket(bucket_name)
            snapshot_present = (
                bucket.blob(f"{prefix}{STATE_FILENAME_GZ}").exists(client)
                or bucket.blob(f"{prefix}{STATE_FILENAME}").exists(client)
            )
        except Exception:  # noqa: BLE001 — best-effort
            snapshot_present = False

    if not snapshot_present:
        # Stale entry — prune and pretend the slot is empty. The next
        # successful save_result will re-populate.
        _log.info(
            "registry_entry_pruned_stale",
            user_key=user_key,
            destination=destination,
            reason="snapshot file missing on disk",
        )
        _prune_registry_slot(user_key)
        return None

    return {
        "destination": destination,
        "output_dir":  entry.get("output_dir", ""),
        "saved_at":    entry.get("saved_at", 0),
    }


def _prune_registry_slot(user_key: str) -> None:
    """Drop one user's slot from the registry. Best-effort — failures
    are logged but never raise."""
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


# ---------------------------------------------------------------------
# file:// backend
# ---------------------------------------------------------------------


def _save_file(destination: str, payload: Dict[str, Any]) -> str:
    """file:// → write gzipped JSON to <dir>/.migrator_state.json.gz.

    Compression typically gives 5-8x size reduction on the snapshot
    blob (most of the bytes are repeated `_source_destination` /
    `_source_filter` / `arguments` strings — JSON whitespace + gzip's
    LZ77 sliding window crush both). Drops the 941-resource customer
    blob from ~3 MB to ~400 KB. Matters for the gs:// backend where
    read latency scales with size.

    Atomic write: dump to .tmp first, then rename. Prevents a half-
    written file from poisoning future loads if the process dies
    mid-write.
    """
    dirpath = destination[len("file://"):]
    os.makedirs(dirpath, exist_ok=True)
    path = os.path.join(dirpath, STATE_FILENAME_GZ)
    tmp = path + ".tmp"
    # Build JSON in memory then gzip-write — gzip needs a single byte
    # stream and json.dump can't write to a binary file directly.
    raw = json.dumps(payload, sort_keys=True, default=_json_default).encode("utf-8")
    with gzip.open(tmp, "wb", compresslevel=6) as f:
        f.write(raw)
    os.replace(tmp, path)
    return path


def _load_file(destination: str) -> Optional[Dict[str, Any]]:
    """file:// → prefer the .gz variant; fall back to legacy uncompressed."""
    dirpath = destination[len("file://"):]
    gz_path = os.path.join(dirpath, STATE_FILENAME_GZ)
    if os.path.isfile(gz_path):
        with gzip.open(gz_path, "rb") as f:
            return json.loads(f.read().decode("utf-8"))
    legacy_path = os.path.join(dirpath, STATE_FILENAME)
    if os.path.isfile(legacy_path):
        with open(legacy_path, "r", encoding="utf-8") as f:
            return json.load(f)
    return None


# ---------------------------------------------------------------------
# gs:// backend (Cloud Run multi-tenant persistence)
# ---------------------------------------------------------------------


def _parse_gs_url(url: str) -> "tuple[str, str]":
    """`gs://bucket/some/prefix/` → (bucket, prefix). Trailing slash on
    the prefix is preserved; callers append the state filename."""
    parsed = urlparse(url)
    bucket = parsed.netloc
    prefix = parsed.path.lstrip("/")
    if prefix and not prefix.endswith("/"):
        prefix += "/"
    return bucket, prefix


def _save_gs(destination: str, payload: Dict[str, Any]) -> str:
    """gs://<bucket>/<prefix>/ → upload gzipped JSON to
    <bucket>/<prefix>/.migrator_state.json.gz.

    Uses google-cloud-storage. ADC (Application Default Credentials)
    work transparently on Cloud Run — no key file needed when the
    service account has roles/storage.objectAdmin on the bucket.
    """
    try:
        from google.cloud import storage  # type: ignore
    except ImportError as e:
        raise RuntimeError(
            "gs:// backend requested but google-cloud-storage is not "
            "installed. Add it to requirements.txt and rebuild the image."
        ) from e

    bucket_name, prefix = _parse_gs_url(destination)
    blob_name = f"{prefix}{STATE_FILENAME_GZ}"
    raw = json.dumps(payload, sort_keys=True, default=_json_default).encode("utf-8")
    gzipped = gzip.compress(raw, compresslevel=6)

    client = storage.Client()
    bucket = client.bucket(bucket_name)
    blob = bucket.blob(blob_name)
    blob.content_encoding = "gzip"
    blob.upload_from_string(gzipped, content_type="application/json")
    return f"gs://{bucket_name}/{blob_name}"


def _load_gs(destination: str) -> Optional[Dict[str, Any]]:
    """gs:// → download + decompress."""
    try:
        from google.cloud import storage  # type: ignore
    except ImportError:
        return None

    bucket_name, prefix = _parse_gs_url(destination)
    client = storage.Client()
    bucket = client.bucket(bucket_name)

    # Prefer the .gz variant; fall back to legacy uncompressed for any
    # pre-compression snapshots in the same bucket.
    for blob_suffix in (STATE_FILENAME_GZ, STATE_FILENAME):
        blob = bucket.blob(f"{prefix}{blob_suffix}")
        if not blob.exists(client):
            continue
        raw = blob.download_as_bytes()
        if blob_suffix.endswith(".gz"):
            raw = gzip.decompress(raw)
        return json.loads(raw.decode("utf-8"))
    return None


# ---------------------------------------------------------------------
# Per-user registry (lets the UI rediscover the last output_dir)
# ---------------------------------------------------------------------


def _registry_dir() -> str:
    """Where the "last run" registry lives.

    Single-user local dev → ``~/.migrator/``.
    Cloud Run → set ``MIGRATOR_REGISTRY_DIR`` to a writable volume (or
    later, swap this whole module to a backend-aware variant that uses
    Firestore / DynamoDB keyed by tenant).
    """
    override = os.environ.get(_REGISTRY_ENV)
    if override:
        return override
    return os.path.join(os.path.expanduser("~"), ".migrator")


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
        # Corrupt registry → treat as empty; next save overwrites it.
        pass
    return {}


def _register_last_run(*, user_key: str, output_dir: str, destination: str) -> None:
    """Update the registry slot for ``user_key`` with this run's location."""
    try:
        os.makedirs(_registry_dir(), exist_ok=True)
        registry = _read_registry()
        registry[user_key] = {
            "destination": destination,
            "output_dir":  output_dir,
            "saved_at":    time.time(),
        }
        path = os.path.join(_registry_dir(), _REGISTRY_FILENAME)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(registry, f, indent=2, sort_keys=True)
        os.replace(tmp, path)
    except OSError as e:
        _log.warning("registry_write_failed", error=str(e))


def _lookup_last_run(*, user_key: str) -> str:
    entry = _read_registry().get(user_key)
    if not entry:
        return ""
    return str(entry.get("destination", ""))


# ---------------------------------------------------------------------
# (de)serialization
# ---------------------------------------------------------------------


def _serialize(result: MigrationResult) -> Dict[str, Any]:
    """MigrationResult → JSON-friendly dict.

    We don't use ``dataclasses.asdict`` directly because the inner
    DiscoveredResource has a non-trivial ``arguments`` map that may
    contain HCL parse leftovers (lists, dicts, primitives — fine for
    JSON) and we want a stable field order for diffing snapshots.
    """
    return {
        "project_id":           result.project_id,
        "repo_path":            result.repo_path,
        "target_cloud":         result.target_cloud,
        "source_iac":           result.source_iac,
        "target_format":        result.target_format,
        "source_cloud":         result.source_cloud,
        "resources":            [dataclasses.asdict(r) for r in result.resources],
        "files_scanned":        result.files_scanned,
        "dep_edges":            [dataclasses.asdict(e) for e in result.dep_edges],
        "confidence":           [dataclasses.asdict(c) for c in result.confidence],
        "output_dir":           result.output_dir,
        "migration_guide_path": result.migration_guide_path,
        "helper_script_paths":  list(result.helper_script_paths),
        "skeleton_paths":       list(result.skeleton_paths),
        "validation":           result.validation,
        "duration_s":           result.duration_s,
        "errors":               list(result.errors),
    }


def _deserialize(payload: Dict[str, Any]) -> MigrationResult:
    """JSON dict → MigrationResult.

    Forward-compat: unknown fields are dropped; missing fields fall back
    to dataclass defaults. The schema_version check upstream catches
    breaking changes.
    """
    resources = [_resource_from(d) for d in payload.get("resources", [])]
    dep_edges = [_edge_from(d) for d in payload.get("dep_edges", [])]
    confidence = [_finding_from(d) for d in payload.get("confidence", [])]

    return MigrationResult(
        project_id=payload.get("project_id"),
        repo_path=payload.get("repo_path", ""),
        target_cloud=payload.get("target_cloud", "aws"),
        source_iac=payload.get("source_iac", "terraform"),
        target_format=payload.get("target_format", "terragrunt"),
        source_cloud=payload.get("source_cloud", "gcp"),
        resources=resources,
        files_scanned=int(payload.get("files_scanned", 0)),
        dep_edges=dep_edges,
        confidence=confidence,
        output_dir=payload.get("output_dir"),
        migration_guide_path=payload.get("migration_guide_path"),
        helper_script_paths=list(payload.get("helper_script_paths", [])),
        skeleton_paths=list(payload.get("skeleton_paths", [])),
        validation=payload.get("validation"),
        duration_s=float(payload.get("duration_s", 0.0)),
        errors=list(payload.get("errors", [])),
    )


def _resource_from(d: Dict[str, Any]) -> DiscoveredResource:
    return DiscoveredResource(
        tf_type=d.get("tf_type", ""),
        name=d.get("name", ""),
        module_path=d.get("module_path", ""),
        file_path=d.get("file_path", ""),
        arguments=d.get("arguments", {}) or {},
        terragrunt_deps=list(d.get("terragrunt_deps", [])),
    )


def _edge_from(d: Dict[str, Any]) -> DependencyEdge:
    return DependencyEdge(
        source=d.get("source", ""),
        target=d.get("target", ""),
        via=d.get("via", ""),
    )


def _finding_from(d: Dict[str, Any]) -> ConfidenceFinding:
    return ConfidenceFinding(
        resource_address=d.get("resource_address", ""),
        tf_type=d.get("tf_type", ""),
        band=d.get("band", ""),
        score_pct=int(d.get("score_pct", 0)),
        aws_equivalent=d.get("aws_equivalent"),
        reason=d.get("reason", ""),
        notes=list(d.get("notes", [])),
    )


def _json_default(o: Any) -> Any:
    """Last-resort fallback for objects json.dump can't handle natively.

    The HCL parser sometimes hands us tuples (which JSON converts to
    lists implicitly) or numeric types like Decimal. We coerce here
    rather than letting the dump fail.
    """
    if isinstance(o, (set, tuple)):
        return list(o)
    if dataclasses.is_dataclass(o):
        return dataclasses.asdict(o)
    return str(o)
