# common/errors.py
"""Typed exceptions shared across the four engines.

Why a shared errors module
--------------------------
Phase 0 audit (punchlist CC-2) flagged that the importer and detector
both shell out to external binaries (``gcloud``, ``terraform``) without
timeouts -- a slow upstream can hang a Cloud Run request indefinitely.
The fix is to wrap every ``subprocess.run`` with an explicit
``timeout=`` and raise a typed exception on expiry so the calling
engine can surface it as a structured failure (failure-modes matrix
row #10) rather than letting it bubble as a generic
``subprocess.TimeoutExpired``.

The same pattern will appear in Phase 3 (Translator Vertex AI
timeouts) and Phase 4 (Detector terraform plan timeouts). Centralising
the exception types here means:

* one place to grep when an operator asks "what does `UpstreamTimeout`
  mean and where does it fire"
* engines can catch a common base (``EngineError``) when they want to
  handle any of our typed errors uniformly
* the Streamlit UI (Phase 6) can format a typed exception into a
  red-banner message with a one-line explanation from the exception's
  ``user_hint`` attribute, rather than showing a stack trace

Design choices
--------------
* **Single base class** (``EngineError``) so UI code can do
  ``except EngineError as e: st.error(e.user_hint)`` regardless of
  whether it was a timeout, auth failure, or preflight failure.
* **Structured fields, not message-parsing.** Every exception carries
  the minimum context needed to debug (``binary``, ``elapsed_s``,
  ``cmd``) as attributes, NOT buried in the formatted message string.
  Structured logs (CC-1) then emit those fields verbatim with
  ``log.error("upstream_timeout", exc_info=True, **e.fields)``.
* **``user_hint`` is the UI-safe message.** Safe to show a customer,
  no paths or internal names. The exception's ``__str__`` is the
  engineer-facing message (more detail) for logs.

When to add a new exception class here
--------------------------------------
* A well-defined terminal failure mode that at least two engines hit,
  or one engine plus the UI needs to format specially.
* If it's only ever raised and caught in one module, keep it local.
"""

from __future__ import annotations

from typing import Any, Optional


class EngineError(Exception):
    """Base class for all engine-level typed errors.

    Subclasses MUST set:
        user_hint: short, UI-safe explanation (shown to customer)

    Subclasses SHOULD set:
        fields: dict of structured context for logging

    The engineer-facing message (``__str__``) is set via the positional
    argument passed to ``super().__init__()`` -- include as much debug
    context as is useful; the ``user_hint`` is what gets shown to
    customers.
    """

    user_hint: str = "An internal error occurred."
    fields: dict[str, Any]

    def __init__(self, message: str, **fields: Any) -> None:
        super().__init__(message)
        self.fields = fields


class UpstreamTimeout(EngineError):
    """Raised when an external binary (gcloud, terraform, conftest) exceeds its timeout.

    Captures enough context to diagnose:
        binary:    "gcloud" | "terraform" | "conftest"
        stage:     what the binary was doing ("init", "plan", "describe", ...)
        elapsed_s: wall-clock seconds until the timeout fired
        timeout_s: the timeout that was set (so operators can tune it)
        cmd:       first token of the command (safe for logs) -- NEVER
                   include full args (may contain tenant IDs or
                   project IDs; log-bind those via ``bind_context``
                   instead)

    Example::

        try:
            subprocess.run(args, timeout=60, ...)
        except subprocess.TimeoutExpired as e:
            raise UpstreamTimeout(
                f"gcloud describe timed out after 60s",
                binary="gcloud",
                stage="describe",
                elapsed_s=60,
                timeout_s=60,
                cmd="gcloud",
            ) from e

    The ``from e`` preserves the original TimeoutExpired in
    ``__cause__`` for debug, without polluting the user-visible
    message with subprocess's raw traceback.
    """

    user_hint = (
        "An upstream service did not respond in time. "
        "This is usually transient -- please retry. "
        "If it persists, the target project may be unreachable "
        "or the request may be larger than the configured timeout."
    )

    def __init__(
        self,
        message: str,
        *,
        binary: str,
        stage: str,
        elapsed_s: float,
        timeout_s: float,
        cmd: Optional[str] = None,
    ) -> None:
        super().__init__(
            message,
            binary=binary,
            stage=stage,
            elapsed_s=elapsed_s,
            timeout_s=timeout_s,
            cmd=cmd or binary,
        )
        self.binary = binary
        self.stage = stage
        self.elapsed_s = elapsed_s
        self.timeout_s = timeout_s
        self.cmd = cmd or binary


class PreflightError(EngineError):
    """Raised when a workflow cannot START because inputs/environment are invalid.

    This is the "A" in the A+D return pattern used by ``run_workflow``:
    preflight failures RAISE (can't even begin), workflow-completed
    results (green, red, or zeros) RETURN a ``WorkflowResult``. Callers
    that couldn't usefully act on a failed-to-start workflow don't need
    to branch on a result shape -- they get a typed exception with
    ``user_hint`` ready for the UI.

    Fires when:
      * project_id fails the demo-lock safety gate (``ValueError`` from
        ``config.resolve_target_project_id``)
      * workdir cannot be resolved (path traversal protection rejects
        a malformed project_id)
      * ``terraform init`` fails (missing provider, lock-file drift,
        registry unreachable) -- operator can't do anything downstream
        without a usable plugin cache

    Intentionally NOT fired for:
      * empty discovery results (the project has no supported resources
        -- workflow completed, just nothing to do; returns a zeroed
        ``WorkflowResult`` instead)
      * user cancels selection menu (same rationale)
      * per-resource HCL generation or plan failures (those land in the
        ``failed`` bucket of the ``WorkflowResult``; workflow still
        completed)

    Carries:
        stage:  short identifier for log filtering. Allowed values pin
                dashboard queries:
                  - "validate_project_id"
                  - "resolve_workdir"
                  - "terraform_init"
                Add new values deliberately; operators filter by exact
                match.
        reason: human-readable one-liner (usually the caught exception's
                str()). Goes into ``.fields`` for structured logs.
    """

    user_hint = (
        "The workflow could not start because the input or environment "
        "is invalid. Please verify your project ID and retry. "
        "If the problem persists, contact your administrator."
    )

    def __init__(
        self,
        message: str,
        *,
        stage: str,
        reason: Optional[str] = None,
    ) -> None:
        super().__init__(
            message,
            stage=stage,
            reason=reason or message,
        )
        self.stage = stage
        self.reason = reason or message
