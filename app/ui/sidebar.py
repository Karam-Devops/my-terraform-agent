# app/ui/sidebar.py
"""Shared sidebar — global project picker + runtime info (PUI-1).

Every page in ``app/pages/`` calls ``render_sidebar()`` at the top so:

  * The project picker is visible and consistent on every page.
  * The picked ``project_id`` is shared across pages via
    ``st.session_state`` (Streamlit's per-session container; survives
    page navigation but isolated between browser tabs / users).
  * The ``gcloud projects list`` call is cached (60-second TTL) so
    page navigation feels instant -- no per-click gcloud round-trip.

Why a function rather than a Streamlit "page" of its own: the
sidebar widget is part of EVERY page's layout. Streamlit's
``pages/`` mechanism doesn't have a "shared chrome" concept; the
canonical pattern is to import a render helper and call it at the
top of each page file. One line per page; no risk of drift.

Picker behaviour:

  * Cached gcloud list -> ``st.selectbox`` of available projects.
    The runtime SA's IAM bindings determine what shows up here, so
    in Stage-1 / dev it's typically just ``dev-proj-470211``; in
    Stage-2 / customer-facing it expands to whatever customer
    projects we've onboarded.
  * Empty list (gcloud unavailable, unauthenticated, or 0 projects):
    fall back to a text input. The operator can still type a value
    and proceed -- useful for local-dev situations where ADC isn't
    set up but the workdir already exists.
  * The selected value lives in ``st.session_state["project_id"]``;
    pages read from there and pass it to engine entry points.

Cache invalidation:

  * 60s TTL -- balances "operator added a new project, want to see it"
    against "don't fire gcloud on every page click". Operators can
    force-refresh the page to bust earlier if they really need to.
"""

from __future__ import annotations

import os
import subprocess
from typing import List

import streamlit as st


@st.cache_data(ttl=60, show_spinner=False)
def _list_gcp_projects() -> List[str]:
    """Return the project IDs the current ADC principal can list.

    Cached for 60 seconds (see module docstring). Returns [] on any
    failure (gcloud missing, unauthenticated, no projects, network) --
    the caller falls back to a text input.

    Doesn't raise: a sidebar that crashes the whole app on a transient
    gcloud hiccup would be terrible UX. Empty list is the universal
    "we couldn't get a list" signal.
    """
    try:
        result = subprocess.run(
            [
                "gcloud", "projects", "list",
                "--filter=lifecycleState:ACTIVE",
                "--format=value(projectId)",
            ],
            capture_output=True, text=True, timeout=10, check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return []
    if result.returncode != 0:
        return []
    raw = result.stdout.strip()
    if not raw:
        return []
    return sorted(line.strip() for line in raw.splitlines() if line.strip())


def render_sidebar() -> str:
    """Render the global sidebar; return the selected ``project_id``.

    Idempotent across pages -- safe to call from every page's top.
    Streamlit's ``key`` parameter on widgets auto-binds the value to
    ``st.session_state``, so the picker remembers its selection when
    the operator navigates between pages.

    Returns:
        The selected project_id string. May be empty if the operator
        hasn't picked anything (callers should handle that with
        ``st.warning(...); st.stop()``).
    """
    with st.sidebar:
        st.markdown("### Project")

        projects = _list_gcp_projects()

        # Pre-populate with TARGET_PROJECT_ID env var default if no
        # selection has been made this session yet. Lets operators
        # land on the page and immediately have the right project
        # selected without an extra click.
        #
        # DEV CONVENIENCE (Phase 8 demo): when neither session_state
        # nor TARGET_PROJECT_ID is set, fall back to "dev-proj-470211"
        # so the demo doesn't require a typed/selected project on
        # every page load. Remove this fallback before shipping to
        # multi-tenant customers — production should require explicit
        # project selection.
        _DEV_PROJECT_FALLBACK = "dev-proj-470211"
        env_default = os.environ.get("TARGET_PROJECT_ID", _DEV_PROJECT_FALLBACK)
        current = st.session_state.get("project_id", env_default)

        if projects:
            # Drop down of cached list. If env_default is in the list,
            # pre-select it; else default to the first entry.
            try:
                default_index = projects.index(current) if current in projects else 0
            except ValueError:
                default_index = 0
            st.selectbox(
                "GCP project",
                options=projects,
                index=default_index,
                key="project_id",
                help="Projects the runtime SA can list. "
                     "Cached for 60s; refresh the page to re-fetch.",
            )
        else:
            # Fallback: free-form text. Local-dev or when gcloud
            # is unavailable. Same session_state key so behavior is
            # identical from the page's POV.
            st.text_input(
                "GCP project",
                value=current,
                key="project_id",
                help="Type the GCP project ID. "
                     "(Couldn't enumerate via gcloud -- check "
                     "ADC / impersonation if you expected a dropdown.)",
            )
            st.caption(
                "ℹ️ gcloud project listing unavailable; using free-text."
            )

        st.markdown("---")
        st.markdown("### Runtime")
        st.caption(f"Host: `{os.environ.get('HOST_PROJECT_ID', '(unset)')}`")
        st.caption(
            f"Bucket: `gs://"
            f"{os.environ.get('MTAGENT_STATE_BUCKET', '(unset)')}/`"
        )
        with st.expander("Environment", expanded=False):
            # Operator-facing diagnostic; same vars as the PSA-2
            # placeholder used to show, just tucked behind an expander
            # so the sidebar stays compact.
            for key in (
                "HOST_PROJECT_ID",
                "MTAGENT_STATE_BUCKET",
                "GCP_LOCATION",
                "MTAGENT_USE_GCS_BACKEND",
                "MTAGENT_PERSIST_SNAPSHOTS",
                "IMPORTER_AUTO_QUARANTINE",
                "MAX_TRANSLATION_WORKERS",
                "MTAGENT_IMPORT_BASE",
            ):
                st.text(f"{key}={os.environ.get(key, '(unset)')}")

    return st.session_state.get("project_id", "")
