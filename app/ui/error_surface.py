# app/ui/error_surface.py
"""Curated error rendering for engine-driven Streamlit pages (PUI-1).

The engines raise two kinds of errors the UI should treat differently:

  * ``PreflightError`` (``common.errors``) -- expected, actionable
    failures (invalid project ID, missing workdir, terraform init
    failure). Each carries a ``user_hint`` describing the next step.
    The UI surfaces the hint prominently; technical details live in
    a collapsible expander.

  * Anything else -- bugs, network blips, unexpected upstream errors.
    The UI shows a generic "something went wrong" banner with a
    collapsible traceback for debugging. Cloud Logging gets the full
    structured log regardless.

Centralising the rendering here keeps every page consistent and makes
the "error happened" UX a one-line call:

    try:
        ...
    except Exception as e:
        render_error(e)
        return

Why no st.exception(e) by default: the raw traceback is great for
us debugging, but presenting it as the primary error surface to
customers (or to vendor demo audiences) looks alarming. The expander
pattern hides it by default while keeping it one click away.
"""

from __future__ import annotations

import traceback

import streamlit as st

from common.errors import PreflightError


def render_error(exc: BaseException, *, context: str = "") -> None:
    """Render an exception in a customer-facing way.

    Branches on exception type:
      * PreflightError -> ``st.error`` with message + ``user_hint``
      * Anything else  -> generic banner + collapsible traceback

    Args:
        exc: The caught exception.
        context: Optional short description of what was being attempted
            (e.g., "running the importer"). Prefixed onto the error
            message so the operator knows which action failed.

    Side-effect: writes to the active Streamlit container. Returns nothing.
    """
    prefix = f"While {context}: " if context else ""

    if isinstance(exc, PreflightError):
        # Curated path. PreflightError is the "expected failure" type
        # -- the engine already knows why it can't proceed and has a
        # human-actionable hint. Surface that prominently.
        message = f"{prefix}{exc.args[0] if exc.args else str(exc)}"
        hint = getattr(exc, "user_hint", None)
        st.error(f"❌ {message}")
        if hint:
            st.info(f"💡 Next step: {hint}")
        # Stage attribute (set by the importer / detector at raise
        # site) names which preflight phase tripped. Operator-facing
        # diagnostic that helps narrow which env / config to check.
        stage = getattr(exc, "stage", None)
        if stage:
            st.caption(f"Stage: `{stage}`")
        return

    # Generic path. We don't know what happened; show a banner that
    # signals "this is unexpected" and tuck the traceback behind an
    # expander so it's available without dominating the screen.
    st.error(
        f"❌ {prefix}an unexpected error occurred.\n\n"
        f"`{type(exc).__name__}: {exc}`\n\n"
        f"Cloud Logging has the full event for diagnosis. "
        f"Expand below for the local traceback."
    )
    with st.expander("Technical details (traceback)", expanded=False):
        st.code(
            "".join(traceback.format_exception(
                type(exc), exc, exc.__traceback__,
            )),
            language="text",
        )
