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

run_button = st.button(
    "Run import",
    type="primary",
    use_container_width=False,
)

if not run_button:
    # Initial render or user hasn't clicked yet; stop here so we don't
    # accidentally fire the import on a page reload triggered by some
    # other widget interaction.
    st.stop()

# --- Live run path -------------------------------------------------------

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
    # render_error branches on PreflightError vs everything else and
    # surfaces the right level of detail. Importantly, persist_on_exit
    # in workdir_context already skipped the GCS write (preserving
    # previous-good state) -- the operator is safe to re-click.
    render_error(e, context="running the importer")
    st.stop()

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
