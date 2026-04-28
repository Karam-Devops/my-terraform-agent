# app/middleware.py
"""Per-request workdir context manager (PSA-4).

Bridges PSA-3's GCS hydrate/persist primitives into the actual
request/action lifecycle. The pattern:

    from app.middleware import workdir_context

    with workdir_context("dev-proj-470211") as workdir:
        # Engines transparently find this dir via the
        # MTAGENT_IMPORT_BASE env var that the middleware sets.
        # GCS state is hydrated before the block runs and persisted
        # back after a successful exit.
        importer.run.run_workflow()

    # On exit:
    #   - SUCCESS: persist local -> GCS
    #   - EXCEPTION: skip persist (preserve previous-good state)
    #   - ALWAYS: env var restored

Lifecycle scoping:

  * Within a single Streamlit session: a (tenant, project) pair
    hydrates ONCE, then subsequent `with workdir_context(same_project)`
    calls reuse the cached local path. This avoids re-rsyncing the
    full ~150MB .terraform/providers/ blob on every UI interaction.

  * Across Streamlit sessions: each session gets its own cache
    (st.session_state-scoped), so two operators (or two tabs from
    the same operator) don't share workdirs. Same-tenant requests
    DO eventually share GCS state -- the cache miss in tab 2
    hydrates from what tab 1 last persisted.

  * Outside Streamlit (CLI, tests): falls back to a module-level
    singleton cache. Safe for single-threaded contexts.

Persist policy:

  * On clean exit -> persist via PSA-3's persist_workdir.
  * On exception -> skip persist. The local mid-error state may be
    inconsistent (partial writes from a failed engine call); we'd
    rather have the customer re-hydrate from previous-good state on
    the next request than overwrite their last-known-good GCS state
    with corrupt partial work.

  * Override available: ``persist_on_exit=False`` for read-only
    operations (e.g. just rendering inventory; no engine ran).

Why this lives in app/ rather than common/:

  * The Streamlit-session-state interaction is UI-layer concern;
    common/ stays Streamlit-agnostic so the engines can be imported
    by non-UI callers (CI, smoke scripts, future Phase 6+ APIs).
  * The fallback module-level singleton makes the middleware usable
    from CLI without dragging in Streamlit -- preserves the
    "Streamlit optional" boundary.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import uuid
from dataclasses import dataclass, field
from typing import Dict, Optional, Tuple

from common.logging import get_logger
from common.storage import hydrate_workdir, persist_workdir

_log = get_logger(__name__)


@dataclass
class WorkdirHandle:
    """Tracks one hydrated workdir for a (tenant, project) pair."""
    tenant_id: str
    project_id: str
    request_uuid: str
    local_path: str


@dataclass
class WorkdirSession:
    """Per-Streamlit-session cache of hydrated workdirs.

    Maps (tenant_id, project_id) -> WorkdirHandle. Each entry is one
    fully-hydrated local /tmp/imported/<uuid>/<project_id>/ tree.
    """
    handles: Dict[Tuple[str, str], WorkdirHandle] = field(default_factory=dict)


# Module-level fallback for non-Streamlit contexts (CLI, unit tests).
# Streamlit context uses st.session_state instead. See _get_session.
_MODULE_SESSION: Optional[WorkdirSession] = None


def _get_session() -> WorkdirSession:
    """Return the per-session WorkdirSession, creating on first access.

    Streamlit context: stored in ``st.session_state["_workdir_session"]``.
    Non-Streamlit context: module-level singleton fallback.

    The fallback path is intentionally permissive -- it lets unit
    tests + CLI smoke scripts use the middleware without spinning
    up a Streamlit server. Production runs always go through the
    Streamlit branch.
    """
    try:
        import streamlit as st
        # Touch session_state to force the lazy init; if we're outside
        # an active Streamlit ScriptRunContext this raises the
        # appropriate exception we catch below.
        if "_workdir_session" not in st.session_state:
            st.session_state["_workdir_session"] = WorkdirSession()
        return st.session_state["_workdir_session"]
    except Exception:
        # Streamlit unavailable / not in a session -- fall back.
        # Catches: ImportError (no streamlit installed), RuntimeError
        # ("missing ScriptRunContext"), and any other Streamlit-version-
        # specific guard error.
        global _MODULE_SESSION
        if _MODULE_SESSION is None:
            _MODULE_SESSION = WorkdirSession()
        return _MODULE_SESSION


def _reset_module_session() -> None:
    """Reset BOTH the module-level fallback AND any Streamlit-side cache.

    For test isolation: each test starts with a fresh WorkdirSession.
    Production code should NOT call this; the session lifecycle is
    request-scoped (Streamlit) or process-scoped (CLI).

    Test context note: outside an actual Streamlit script run,
    ``st.session_state`` silently works (with "missing ScriptRunContext"
    warnings) and uses a process-wide default container that
    PERSISTS across test invocations. So a test-only reset must
    clear both the module fallback AND any handle written into
    Streamlit's container, otherwise tests share state.
    """
    global _MODULE_SESSION
    _MODULE_SESSION = None
    try:
        import streamlit as st
        if "_workdir_session" in st.session_state:
            del st.session_state["_workdir_session"]
    except Exception:
        # Streamlit unavailable -- module fallback was the only cache;
        # already cleared above.
        pass


@contextlib.contextmanager
def workdir_context(
    project_id: str,
    *,
    tenant_id: Optional[str] = None,
    persist_on_exit: bool = True,
):
    """Context manager: hydrate workdir on enter, persist + cleanup on exit.

    Usage::

        with workdir_context("dev-proj-470211") as workdir:
            # `workdir` is /tmp/imported/<uuid>/dev-proj-470211/
            # MTAGENT_IMPORT_BASE is set to /tmp/imported/<uuid>/
            # so common.workdir.resolve_project_workdir resolves to
            # exactly `workdir` for the engines.
            importer.run.run_workflow()

    Args:
        project_id: GCP project ID (strict-validated by storage.py).
        tenant_id: Multi-tenant identifier. Defaults to ``"default"``
            for Round-1 single-tenant. Multi-tenant SaaS wires real
            tenant_id from the IAP token.
        persist_on_exit: If True (default), sync local -> GCS on
            successful exit. Set False for read-only operations.

    Yields:
        Absolute local path to the hydrated workdir.

    Raises:
        ValueError: project_id / tenant_id failed validation.
        subprocess.CalledProcessError: GCS hydrate/persist failed
            (network, perms, bucket missing).
        Whatever the engine code raises propagates; persist is
            skipped on any exception (preserving previous-good state).
    """
    tenant = tenant_id or "default"
    session = _get_session()
    cache_key = (tenant, project_id)

    if cache_key in session.handles:
        # Cache hit: reuse existing hydrated workdir. No re-hydrate
        # cost; same /tmp tree the previous action saw.
        handle = session.handles[cache_key]
        _log.info(
            "workdir_context_cache_hit",
            tenant_id=tenant,
            project_id=project_id,
            request_uuid=handle.request_uuid,
            local_path=handle.local_path,
        )
        # Re-set MTAGENT_IMPORT_BASE in case a prior context raised
        # before its finally-block restored it (defensive).
        prev_base = os.environ.get("MTAGENT_IMPORT_BASE")
        os.environ["MTAGENT_IMPORT_BASE"] = os.path.dirname(handle.local_path)
        try:
            yield handle.local_path
        except Exception:
            raise
        else:
            if persist_on_exit:
                persist_workdir(
                    handle.local_path, project_id, tenant_id=tenant,
                )
        finally:
            if prev_base is None:
                os.environ.pop("MTAGENT_IMPORT_BASE", None)
            else:
                os.environ["MTAGENT_IMPORT_BASE"] = prev_base
        return

    # Cache miss: fresh hydrate.
    # Short UUID (8 hex chars) -- collision risk negligible for our
    # single-tenant Round-1 scale; full UUID would clutter the path
    # display in logs / UI.
    request_uuid = uuid.uuid4().hex[:8]
    base = f"/tmp/imported/{request_uuid}"

    # Engines that read MTAGENT_IMPORT_BASE find their workdir under
    # this UUID-scoped root. Set BEFORE hydrate so the resolved local
    # path is consistent with what the engines will derive.
    prev_base = os.environ.get("MTAGENT_IMPORT_BASE")
    os.environ["MTAGENT_IMPORT_BASE"] = base

    try:
        local_path = hydrate_workdir(
            project_id, tenant_id=tenant, local_root=base,
        )
    except Exception:
        # Hydrate failed; restore env BEFORE re-raising so we don't
        # leak a stale env var into the caller's process.
        if prev_base is None:
            os.environ.pop("MTAGENT_IMPORT_BASE", None)
        else:
            os.environ["MTAGENT_IMPORT_BASE"] = prev_base
        raise

    handle = WorkdirHandle(
        tenant_id=tenant,
        project_id=project_id,
        request_uuid=request_uuid,
        local_path=local_path,
    )
    session.handles[cache_key] = handle

    _log.info(
        "workdir_context_hydrated",
        tenant_id=tenant,
        project_id=project_id,
        request_uuid=request_uuid,
        local_path=local_path,
    )

    try:
        yield local_path
    except Exception:
        # Don't persist on error; preserve previous-good state in GCS
        raise
    else:
        if persist_on_exit:
            persist_workdir(local_path, project_id, tenant_id=tenant)
    finally:
        if prev_base is None:
            os.environ.pop("MTAGENT_IMPORT_BASE", None)
        else:
            os.environ["MTAGENT_IMPORT_BASE"] = prev_base


def cleanup_session_workdirs() -> None:
    """Remove all session-cached local /tmp dirs.

    Called manually before container shutdown OR at session end (if
    Streamlit grows session-end hooks). Persisted GCS state is NOT
    affected -- only the local /tmp/<uuid>/ trees are removed.

    Best-effort: errors during rmtree are logged + swallowed so a
    permission glitch doesn't leak the cache or block shutdown.
    Cloud Run's ephemeral /tmp is wiped between container instances
    anyway, so this is more about freeing memory mid-session than
    preventing real disk leaks.
    """
    session = _get_session()
    if not session.handles:
        return

    for handle in session.handles.values():
        # The /tmp/<uuid>/ root is the parent of local_path
        # (local_path = /tmp/imported/<uuid>/<project_id>).
        # Removing the parent removes the project subdir too.
        parent = os.path.dirname(handle.local_path)
        if os.path.isdir(parent):
            try:
                shutil.rmtree(parent)
                _log.info(
                    "workdir_cleanup_complete",
                    tenant_id=handle.tenant_id,
                    project_id=handle.project_id,
                    request_uuid=handle.request_uuid,
                    local_path=parent,
                )
            except OSError as e:
                _log.warning(
                    "workdir_cleanup_failed",
                    tenant_id=handle.tenant_id,
                    project_id=handle.project_id,
                    request_uuid=handle.request_uuid,
                    local_path=parent,
                    error=str(e),
                )

    session.handles.clear()
