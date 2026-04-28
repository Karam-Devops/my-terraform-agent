# app/pages/1_Importer.py
"""Importer page (PUI-1).

UX:
  1. Sidebar shows the global project picker (rendered by render_sidebar).
  2. Body shows a "Run import" button + an explainer of what will happen.
  3. Click -> spinner with elapsed time -> result card.
  4. Errors render via ``app.ui.error_surface.render_error`` so
     PreflightError's user_hint shows prominently and other exceptions
     get a collapsible traceback.

Backend wiring:

  * ``workdir_context(project_id)`` from ``app.middleware`` (PSA-4)
    hydrates the per-project workdir from GCS on entry, persists on
    successful exit, and skips persist on exception (preserves
    previous-good state per PSA-3 / PSA-4 contract).
  * ``importer.run.run_workflow(project_id, selected_indices="all")``
    (the PUI-1-prep refactor) bypasses the CLI's stdin prompts.
  * Snapshot persistence (PSA-9) fires automatically inside
    run_workflow's existing try/except block; UI doesn't need to
    explicitly call it.
  * On success the result card renders the WorkflowResult.as_fields()
    counts. The structured details (per-resource outcomes) are
    available in Cloud Logging for now -- a richer viewer is PUI-6
    polish.

Why selected_indices="all" for v1:

  * The Streamlit checkbox-grid for per-resource selection is PUI-6
    polish work; PUI-1's contract is "operator picks a project, app
    imports everything supported in that project". Maps to the most
    common use case (initial onboarding) and keeps PUI-1 small.
"""

import time

import streamlit as st

from app.ui.sidebar import render_sidebar
from app.ui.error_surface import render_error


# Page chrome
st.set_page_config(
    page_title="mtagent · Importer",
    page_icon="📥",
    layout="wide",
)

# Always render the sidebar first; it owns the project_id.
project_id = render_sidebar()

st.title("📥 Importer")
st.caption(
    "Discover supported GCP resources and generate Terraform "
    "configuration + state for each one."
)

# Guard: no project picked -> render guidance and stop. The empty
# project_id case is most common on first page load before the
# operator has chosen anything (or when gcloud listing failed and
# they haven't typed a value yet).
if not project_id:
    st.warning(
        "Pick a project in the sidebar to get started.",
        icon="⚠️",
    )
    st.stop()

# Show what will happen + the current selection.
st.markdown(f"**Project:** `{project_id}`")

st.markdown(
    "Click **Run import** below. The workflow will:\n"
    "1. Discover supported GCP resources in the project (parallel scan).\n"
    "2. For each, generate the matching Terraform `.tf` file.\n"
    "3. Run `terraform import` to populate state.\n"
    "4. Validate; quarantine anything that won't import cleanly "
    "(no interactive prompts in this UI -- per-resource debugging "
    "lives in Cloud Logging for v1)."
)
st.caption(
    "Selection scope: imports **every** supported resource discovered "
    "in this project. A per-resource picker is planned for PUI-6."
)

# --- PUI-1 Tier-A run lock (prevents same-session double-runs) -----------
#
# Streamlit's blocking model already prevents same-tab double-clicks
# (the page can't process a click while a script rerun is mid-execution).
# What it DOESN'T prevent: a browser refresh during the run, which
# interrupts the workflow and -- without a lock -- would happily start a
# fresh run on the next click.
#
# Tier A: session_state-scoped run lock with a stale-detection timeout.
#
#   * On click: store {start_ts, project_id} in session_state.
#   * On every subsequent rerun in the SAME session: if the lock is
#     present and fresher than RUN_TIMEOUT_S, render a disabled button +
#     "in progress" banner and stop. Otherwise, lock is stale (refresh
#     killed the previous run) -- clear it.
#   * On clean run completion (success OR caught exception): clear the
#     lock in `finally`.
#
# What this PROTECTS: same-session refresh-during-run.
# What this does NOT protect: multi-tab (different sessions), multi-user
# on the same project. Those need a server-side lock (Tier B, GCS-backed)
# -- queued in the polish backlog (PUI-6).
#
# Why a button placeholder pattern: in Streamlit, you can't "update" a
# rendered button in-place. Wrapping it in st.empty() lets us swap the
# primary "Run import" for a disabled "Running..." once the click fires,
# without restructuring the page flow.

import time as _time  # local alias; the module-level `import time`
                       # above is used by the duration measurement below

# 10 min: longer than any realistic single-project import. Stale-clears
# the lock if a previous run was interrupted (browser refresh, container
# restart) so the operator isn't permanently blocked.
_RUN_TIMEOUT_S = 600

# Stale-clear before rendering: handles the "previous run was interrupted"
# case so a refresh doesn't leave the page stuck claiming "in progress".
_lock = st.session_state.get("_importer_run_lock")
if _lock is not None:
    _elapsed = _time.time() - _lock.get("start_ts", 0)
    if _elapsed > _RUN_TIMEOUT_S:
        # Stale -- previous run was almost certainly interrupted before
        # `finally` could clear the lock. Reset and proceed normally.
        st.session_state.pop("_importer_run_lock", None)
        _lock = None

# Render the button slot. We use st.empty() so we can swap its content
# without re-rendering the whole page.
button_slot = st.empty()

if _lock is not None:
    # An import is in progress in THIS session. Show disabled button +
    # banner, don't allow a re-click. Project_id from the lock surfaces
    # the (rare but possible) case where the operator switched projects
    # mid-run; the banner tells them which one is actually running.
    locked_project = _lock.get("project_id", "unknown")
    elapsed_s = int(_time.time() - _lock.get("start_ts", 0))
    button_slot.button(
        f"Running ({elapsed_s}s)…",
        disabled=True,
        type="secondary",
        key="run_import_disabled_locked",
    )
    st.warning(
        f"⏳ Import in progress for **{locked_project}** in this "
        f"session (started {elapsed_s}s ago). Wait for it to complete; "
        f"if the tab was refreshed, the lock auto-clears after "
        f"{_RUN_TIMEOUT_S // 60} minutes.",
        icon="⏳",
    )
    st.stop()

run_button = button_slot.button(
    "Run import",
    type="primary",
    use_container_width=False,
    key="run_import_active",
)

if not run_button:
    # Initial render or user hasn't clicked yet; stop here so we don't
    # accidentally fire the import on a page reload triggered by some
    # other widget interaction.
    st.stop()

# --- Live run path -------------------------------------------------------

# Acquire the lock IMMEDIATELY on click so a refresh during the long
# blocking workflow lands on the "already in progress" branch above.
st.session_state["_importer_run_lock"] = {
    "start_ts": _time.time(),
    "project_id": project_id,
}
# Visual swap: the button-slot now shows a disabled "Running..." indicator
# while the workflow blocks. Operator gets immediate feedback that the
# click was registered and the action is in flight.
button_slot.button(
    "Running…", disabled=True, type="secondary",
    key="run_import_disabled_active",
)

# Lazy imports: heavy modules (importer pulls in google-cloud SDKs,
# vertexai, etc.) only loaded once an actual run starts. Keeps the
# initial page render snappy and isolates engine import failures from
# the empty-state UX.
from app.middleware import workdir_context  # noqa: E402
from importer.run import run_workflow  # noqa: E402

started = time.monotonic()

try:
    with st.spinner(
        f"Importing project '{project_id}' ... "
        f"(this may take 30s–5min depending on project size)"
    ):
        with workdir_context(project_id) as workdir:
            # selected_indices="all" -> auto-select every discovered
            # resource (PUI-1 v1 contract). The workdir is hydrated
            # from GCS on entry, persisted on successful exit.
            result = run_workflow(
                project_id=project_id,
                selected_indices="all",
            )
except Exception as e:  # noqa: BLE001 -- intentionally broad
    # Clear the lock BEFORE rendering the error so the operator can
    # immediately retry. PSA-4's persist_on_exit already skipped the
    # GCS write on exception, so previous-good state is preserved.
    st.session_state.pop("_importer_run_lock", None)
    # render_error branches on PreflightError vs everything else and
    # surfaces the right level of detail.
    render_error(e, context="running the importer")
    st.stop()

# Clean exit: clear the lock so the next click can fire.
st.session_state.pop("_importer_run_lock", None)

duration = time.monotonic() - started

# --- Success: render result card ----------------------------------------

st.success(
    f"✅ Import completed in {duration:.1f}s",
    icon="✅",
)

# Pull the engine's structured fields. as_fields() returns the same
# dict that gets written to the snapshot (PSA-9), so what the
# Dashboard will eventually show matches what the operator sees here.
fields = result.as_fields()

# Hero metrics: imported / needs_attention / skipped / failed.
# Mirrors what Firefly's "import wizard" surfaces post-run; the four
# buckets together account for every selected resource.
m1, m2, m3, m4 = st.columns(4)
m1.metric("Imported", fields.get("imported", 0))
m2.metric("Needs attention", fields.get("needs_attention", 0))
m3.metric("Skipped", fields.get("skipped", 0))
m4.metric("Failed", fields.get("failed", 0))

# Full structured payload for operators / debugging. JSON view is
# native to Streamlit (collapsible), so no formatting work needed.
with st.expander("Full result (structured)", expanded=False):
    st.json(fields)

st.caption(
    "Snapshot of this run is persisted to GCS (see PSA-9). "
    "The Dashboard (PUI-2) will read it without re-running the engine."
)
