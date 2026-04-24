# common/logging.py
"""Structured logging for all four engines.

Why this module exists
----------------------
Before this module, every engine used bare ``print()``. On Cloud Run
that lands in Cloud Logging as unstructured text -- no way to filter
"show me tenant X's requests" or "every import that timed out in the
last 24h". The Phase 0 SaaS audit flagged this as FAIL across all four
engines (punchlist item CC-1).

The contract: every log line carries the engine + request context
(tenant_id, project_id, request_id, stage) as structured fields, so
Cloud Logging's LogQL-style filter can slice by any of them::

    resource.labels.service_name="mtagent" AND jsonPayload.tenant_id="acme-corp"

Design choices
--------------
* **structlog, not stdlib logging alone.** Stdlib's ``extra={}``
  dictionary ergonomics are awful at call sites; structlog's
  ``log.info("event", key=value, ...)`` is clean. structlog still
  *uses* stdlib logging as its backend, so third-party libraries
  emitting via stdlib (e.g. urllib3) flow through the same pipeline.
* **JSON renderer in prod, pretty renderer in dev.** Dev is detected
  by ``MTAGENT_LOG_FORMAT=console`` OR an attached TTY on stdout.
  Cloud Run's stdout is not a TTY, so JSON wins automatically in
  production with no env-var wrangling.
* **Context is bound via ``contextvars``, not passed through calls.**
  ``bind_context(tenant_id=..., project_id=...)`` at the request
  boundary; every log line inside the request inherits the context
  automatically. Uses structlog's ``contextvars`` integration, which
  is asyncio-safe (works with FastAPI/Starlette if we ever add one)
  and thread-safe (works with Streamlit's threading model).
* **Timestamps in UTC, ISO-8601.** Cloud Logging auto-parses
  ``timestamp`` / ``severity`` if the JSON has them in the right
  shape; we emit the shape it wants.

Call-site ergonomics
--------------------
At a module top::

    from common.logging import get_logger
    log = get_logger(__name__)

At a request boundary (CLI entry, Streamlit handler, Cloud Run
handler)::

    from common.logging import bind_context, clear_context
    bind_context(
        engine="importer",
        tenant_id=tenant_id,
        project_id=project_id,
        request_id=request_id,
    )
    try:
        run_workflow(...)
    finally:
        clear_context()

Inside the engine::

    log.info("import_started", resource_type="google_compute_instance")
    log.warning("skipped_resource", reason="403 Forbidden", resource=name)
    log.error("terraform_failed", error=err, stage="plan")

The event name (first positional arg) is the stable identifier --
dashboards and alerts key off it. Keep it ``snake_case`` and stable;
do NOT embed dynamic values in it (``"imported 12 resources"`` BAD;
``"import_complete"`` + ``count=12`` GOOD).
"""

from __future__ import annotations

import logging
import os
import sys
from typing import Any

import structlog

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

# Log level is driven by MTAGENT_LOG_LEVEL (INFO by default). Setting
# DEBUG floods logs -- useful for local debugging, disastrous on Cloud
# Run. Production override lives in the Cloud Run service env, not here.
_DEFAULT_LEVEL = os.environ.get("MTAGENT_LOG_LEVEL", "INFO").upper()

# Format selection:
#   MTAGENT_LOG_FORMAT=json    -> JSON-lines (Cloud Run / CI / file redirect)
#   MTAGENT_LOG_FORMAT=console -> Pretty ANSI-coloured (local dev)
# If unset: auto-detect from stdout. TTY -> console, non-TTY -> json.
# Cloud Run's stdout is never a TTY, so JSON wins automatically there.
_FORMAT = os.environ.get("MTAGENT_LOG_FORMAT", "").lower()
if _FORMAT not in ("json", "console"):
    _FORMAT = "console" if sys.stdout.isatty() else "json"


def _configure() -> None:
    """Configure structlog once per process.

    Idempotent: calling multiple times is a no-op (structlog keeps the
    most recent config). Called at module import so the first
    ``get_logger()`` call already returns a configured logger --
    callers don't have to remember a bootstrap step.
    """
    # Stdlib logging backend: level filter + stdout handler.
    # structlog's processors do the actual formatting; stdlib just
    # routes the final rendered record to stdout.
    logging.basicConfig(
        format="%(message)s",  # structlog already formatted it
        stream=sys.stdout,
        level=getattr(logging, _DEFAULT_LEVEL, logging.INFO),
    )

    # Processors run in order on every log call. Order matters:
    #   1. contextvars merge -- pull tenant_id/project_id/etc from
    #      the current context (set via bind_context).
    #   2. log level / logger name -- surface as first-class fields.
    #   3. timestamp -- UTC ISO-8601; Cloud Logging parses this.
    #   4. stack info / exception rendering -- if log.exception() was
    #      called, expand traceback into the record.
    #   5. renderer -- final step, picks JSON or console.
    shared_processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]

    if _FORMAT == "json":
        renderer: Any = structlog.processors.JSONRenderer()
    else:
        # Colors on in dev only. Safe on Windows (structlog handles
        # ANSI via colorama if available, plain otherwise).
        renderer = structlog.dev.ConsoleRenderer(colors=sys.stdout.isatty())

    structlog.configure(
        processors=shared_processors + [renderer],
        wrapper_class=structlog.stdlib.BoundLogger,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )


_configure()


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    """Return a logger bound to ``name`` (typically ``__name__``).

    The returned logger inherits all context set via ``bind_context``
    at log time -- you do NOT need to pass tenant/project through to
    every function. Context is merged per-call from contextvars.

    Why pass ``__name__``: it surfaces as ``logger`` in the JSON
    record, so filters like
    ``jsonPayload.logger:"importer.terraform_client"`` work in Cloud
    Logging without grepping.
    """
    return structlog.get_logger(name)


def bind_context(**kwargs: Any) -> None:
    """Set context variables visible to every subsequent log call in this task.

    Idiomatic use at request boundaries::

        bind_context(
            engine="importer",
            tenant_id=tenant_id,
            project_id=project_id,
            request_id=request_id,  # uuid4().hex[:8] works fine
        )

    Reserved keys (appear in every line when set):

        - engine      : "importer" | "translator" | "detector" | "policy"
        - tenant_id   : SaaS tenant (None for single-tenant dev)
        - project_id  : GCP project being operated on
        - request_id  : per-request correlation ID
        - stage       : optional sub-stage within a workflow
                        ("scan", "codify", "plan", "apply", ...)

    Ad-hoc keys are fine too -- anything passed here is visible to
    every ``log.info/warning/error`` call until cleared.

    Thread/async safety: uses ``contextvars``. Each asyncio task or
    thread gets its own snapshot; you do NOT have to worry about
    tenant A's context leaking into tenant B's concurrent request.
    """
    structlog.contextvars.bind_contextvars(**kwargs)


def clear_context() -> None:
    """Clear all bound context variables for the current task.

    Call this in a ``finally:`` at the request boundary so stale
    context from a failed request does not bleed into the next one.
    In Cloud Run this matters most when a single container serves
    many requests back-to-back.
    """
    structlog.contextvars.clear_contextvars()


def unbind_context(*keys: str) -> None:
    """Remove specific context keys without clearing all of them.

    Handy mid-request when transitioning stages::

        bind_context(stage="scan")
        ...do scan work...
        unbind_context("stage")
        bind_context(stage="codify")
    """
    structlog.contextvars.unbind_contextvars(*keys)
