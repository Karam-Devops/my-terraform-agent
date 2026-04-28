# app/pages/1_Importer.py
"""Importer page (PUI-1 + PUI-1B per-resource picker).

Two-stage workflow matching the CLI's behaviour:

  Stage A — DISCOVER:
    Operator clicks "Discover resources". The page calls
    inventory.inventory(project_id) which fires the Cloud Asset
    Inventory SDK (PERF-T0). Cheap (~3-8s, no LLM cost). Result list
    is stored in st.session_state and rendered as a checkbox grid.

  Stage B — SELECT + IMPORT:
    Operator picks a subset via checkboxes (defaults: NONE selected,
    so a fat-finger Run import never fires the LLM cost). Click
    "Run import (N selected)". The page calls run_workflow with
    selected_indices=[1, 5, 12, ...] -- the same 1-indexed shape the
    CLI's Stage-2 numbered menu uses. Each selected resource gets
    described + LLM-generated HCL + terraform import.

Why two stages: each LLM call costs ~$0.10-0.50 and takes 10-30s.
With 80 discovered resources, all-import = $8-40 + 10-25 min per
click. PUI-1B v1 makes that cost-controlled by default.

Backend wiring (unchanged from PUI-1):
  * workdir_context (PSA-4) hydrates from GCS on entry, persists on
    successful exit.
  * run_workflow(project_id, selected_indices=[...]) uses the
    PUI-1-prep programmatic-input contract.
  * Snapshot persistence (PSA-9) fires inside run_workflow.

Tier-A run lock + render_error scaffolding from PUI-1 stays intact;
Stage B inherits both.
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

project_id = render_sidebar()

st.title("📥 Importer")
st.caption(
    "Discover supported GCP resources, then pick which ones to "
    "codify as Terraform."
)

# Guard: no project picked
if not project_id:
    st.warning(
        "Pick a project in the sidebar to get started.",
        icon="⚠️",
    )
    st.stop()

st.markdown(f"**Project:** `{project_id}`")

# Session-state keys we own (scope all under one prefix to avoid collisions)
_SS_DISCOVERED = f"_importer_discovered_{project_id}"
_SS_RUN_LOCK = "_importer_run_lock"
_SS_LAST_RESULT = f"_importer_last_result_{project_id}"

# --- Tier-A run lock -----------------------------------------------------
# Same shape as PUI-1 (commit ea32ba2). Carries through to the new 2-stage
# flow; the lock guards the IMPORT click (Stage B), not the cheap
# Discover click.
import time as _time
_RUN_TIMEOUT_S = 600
_lock = st.session_state.get(_SS_RUN_LOCK)
if _lock is not None:
    _elapsed = _time.time() - _lock.get("start_ts", 0)
    if _elapsed > _RUN_TIMEOUT_S:
        st.session_state.pop(_SS_RUN_LOCK, None)
        _lock = None


# --- Stage A: Discover ---------------------------------------------------

st.markdown("---")
st.markdown("### Step 1 — Discover")

discovered = st.session_state.get(_SS_DISCOVERED)

discover_col, refresh_col = st.columns([1, 1])
with discover_col:
    discover_clicked = st.button(
        "🔍 Discover resources" if not discovered else "Re-discover",
        type="primary" if not discovered else "secondary",
        disabled=(_lock is not None),
        key="discover_btn",
        help=(
            "Lists all importer-supported resources in the project via "
            "Cloud Asset Inventory. Cheap (~3-8s); no LLM cost."
        ),
    )
with refresh_col:
    if discovered:
        if st.button("🗑️ Clear discovery", key="clear_discovery_btn"):
            st.session_state.pop(_SS_DISCOVERED, None)
            st.rerun()

if discover_clicked:
    if _lock is not None:
        st.warning("Import in progress; wait for it to complete.", icon="⏳")
        st.stop()
    # Lazy imports for heavy modules (only load when button actually clicked)
    from importer.inventory import inventory as _do_inventory  # noqa: E402

    started = _time.time()
    try:
        with st.spinner(f"Discovering resources in '{project_id}' …"):
            # raise_on_error=False: per-asset-type failures land as
            # warnings (logged) but the discover continues. Operator
            # sees the partial list; missing types are visible in
            # Cloud Logging.
            resources = _do_inventory(project_id, raise_on_error=False)
        # Cache the raw_asset dicts in session_state. We store dicts
        # (not CloudResource dataclasses) because session_state survives
        # reruns more cleanly with plain dicts and we need the dict
        # shape to feed into run_workflow's selected_indices flow anyway.
        st.session_state[_SS_DISCOVERED] = [
            {
                "name": r.raw_asset.get("name", ""),
                "displayName": r.raw_asset.get("displayName") or r.cloud_name,
                "assetType": r.asset_type,
                "tfType": r.tf_type,
                "location": r.location or "—",
                "cloud_name": r.cloud_name,
                "raw_asset": r.raw_asset,
            }
            for r in resources
        ]
        # Clear any stale prior result so the page doesn't show old metrics
        st.session_state.pop(_SS_LAST_RESULT, None)
        elapsed = _time.time() - started
        st.success(
            f"Discovered {len(resources)} resource(s) in {elapsed:.1f}s",
            icon="✅",
        )
        st.rerun()
    except Exception as e:  # noqa: BLE001
        render_error(e, context="discovering resources")
        st.stop()

# After Discover (or on subsequent reruns when we have data in session)
discovered = st.session_state.get(_SS_DISCOVERED)
if not discovered:
    st.info(
        "Click **Discover resources** above to see what's importable in "
        "this project. The list is cached in this session until you "
        "Re-discover or Clear.",
        icon="ℹ️",
    )
    st.stop()

# --- Stage B: Pick + Import ---------------------------------------------

st.markdown("---")
st.markdown(
    f"### Step 2 — Pick resources to import "
    f"({len(discovered)} discovered)"
)
st.caption(
    "Selection defaults to NONE so a fat-finger Run never fires LLM "
    "cost. Use the column header checkbox to select all visible rows."
)

# Build a DataFrame-like list of dicts for st.data_editor's checkbox grid.
# Streamlit's data_editor works well with a dict-of-lists OR a list-of-dicts.
# For 80-row scale a list-of-dicts is fine and avoids a pandas dep here.
import pandas as pd  # already in requirements via streamlit

# Build the editable table. The "Select" column is the only editable one;
# everything else is read-only.
table_rows = []
for idx, r in enumerate(discovered):
    table_rows.append({
        "Select": False,
        "#": idx + 1,  # 1-indexed to match CLI menu numbering
        "Name": r["displayName"] or r["cloud_name"],
        "Type": r["tfType"],  # show the friendly tf_type, not the URN
        "Location": r["location"],
    })
df = pd.DataFrame(table_rows)

# Filter-by-type widget. Helps when the project has many resources of
# many types -- operator can narrow before selecting.
all_tf_types = sorted({r["tfType"] for r in discovered})
filter_col, count_col = st.columns([3, 1])
with filter_col:
    type_filter = st.multiselect(
        "Filter by type",
        options=all_tf_types,
        default=[],
        placeholder="Show all types",
        key="type_filter",
    )
if type_filter:
    df = df[df["Type"].isin(type_filter)]
with count_col:
    st.metric("Visible", len(df))

# Render the checkbox grid via data_editor.
edited_df = st.data_editor(
    df,
    column_config={
        "Select": st.column_config.CheckboxColumn(
            "Select", default=False, width="small",
        ),
        "#": st.column_config.NumberColumn(width="small"),
        "Name": st.column_config.TextColumn(width="large"),
        "Type": st.column_config.TextColumn(width="medium"),
        "Location": st.column_config.TextColumn(width="medium"),
    },
    disabled=("#", "Name", "Type", "Location"),
    hide_index=True,
    use_container_width=True,
    key="resource_picker",
)

# The data_editor returns the edited DataFrame. Pull selected indices
# (1-indexed) from the rows where Select=True.
selected_indices = edited_df.loc[
    edited_df["Select"], "#"
].tolist()

st.markdown("---")
import_col, info_col = st.columns([1, 2])
with import_col:
    import_button = st.button(
        f"▶ Run import ({len(selected_indices)} selected)" if not _lock
        else f"Running ({int(_time.time() - _lock['start_ts'])}s)…",
        type="primary",
        disabled=(len(selected_indices) == 0 or _lock is not None),
        key="run_import_btn",
        use_container_width=True,
    )
with info_col:
    if _lock is not None:
        st.warning(
            f"⏳ Import in progress for **{_lock.get('project_id')}**; "
            f"started {int(_time.time() - _lock['start_ts'])}s ago. "
            f"Wait or refresh after {_RUN_TIMEOUT_S // 60} min if stuck.",
            icon="⏳",
        )
    elif len(selected_indices) == 0:
        st.caption(
            "Select at least one resource above to enable Run import."
        )
    else:
        # Cost / time estimate matching the per-resource ~10-30s LLM cost.
        est_min_s = max(30, len(selected_indices) * 10)
        est_max_s = len(selected_indices) * 30
        st.caption(
            f"Estimated {est_min_s}–{est_max_s}s for "
            f"{len(selected_indices)} resource(s) "
            f"(~10-30s per resource for LLM HCL generation)."
        )

# Show last result if present, BEFORE consuming a fresh click. Lets the
# operator see the prior outcome while picking the next batch.
last_result = st.session_state.get(_SS_LAST_RESULT)
if last_result and not import_button:
    st.markdown("---")
    st.markdown("### Last import result")
    st.success(
        f"✅ Last run completed in {last_result.get('duration_s', 0):.1f}s",
        icon="✅",
    )
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Imported", last_result.get("imported", 0))
    m2.metric("Needs attention", last_result.get("needs_attention", 0))
    m3.metric("Skipped", last_result.get("skipped", 0))
    m4.metric("Failed", last_result.get("failed", 0))
    with st.expander("Full result (structured)", expanded=False):
        st.json(last_result)

# --- PUI-1B v2: Generated HCL viewer (Option A) -------------------------
# Always shown when the project has any persisted .tf files, NOT gated
# on `last_result`. Reasons:
#   * Operator returns to the page after closing the tab -- session_state
#     is gone, last_result is None, but the .tf files are still in GCS.
#     They want to see what was generated last time.
#   * Multi-step demo: pick + import resource A, then come back later
#     to pick + import resource B. The files from A should still be
#     visible in the meantime.
#
# Cost concern: list_workdir_tf_files makes 1 GCS API call per page
# render. For a project with N .tf files, expanding each makes another
# call. Cheap (KB-scale objects, ms latency) and only fires when the
# operator opens the section. Cached via session_state below to avoid
# re-listing on data_editor click reruns.
st.markdown("---")
_SS_TF_FILES = f"_importer_tf_files_{project_id}"
with st.expander(
    "📄 Generated Terraform files (from last successful import)",
    expanded=False,
):
    refresh_col, _spacer_col = st.columns([1, 4])
    with refresh_col:
        if st.button("↻ Refresh list", key="refresh_tf_files_btn"):
            st.session_state.pop(_SS_TF_FILES, None)

    # Lazy fetch: only call GCS if we don't have a cached list this session.
    if _SS_TF_FILES not in st.session_state:
        try:
            from common.storage import list_workdir_tf_files  # noqa: E402
            st.session_state[_SS_TF_FILES] = list_workdir_tf_files(
                project_id,
            )
        except Exception as e:  # noqa: BLE001
            st.warning(
                f"Couldn't list generated files: "
                f"`{type(e).__name__}: {e}`",
                icon="⚠️",
            )
            st.session_state[_SS_TF_FILES] = []

    tf_files = st.session_state.get(_SS_TF_FILES, [])
    if not tf_files:
        st.caption(
            "No generated files yet. Run an import above to populate "
            "this section."
        )
    else:
        st.caption(
            f"{len(tf_files)} file(s). Click any name to view its "
            "HCL content."
        )
        # Per-file accordion: name + size + content + download.
        # Lazy-loads file content only when expander is opened (one
        # GCS GET per opened file).
        from common.storage import read_workdir_file  # noqa: E402
        for tf_file in tf_files:
            fname = tf_file["name"]
            size_kb = tf_file["size_bytes"] / 1024
            with st.expander(
                f"`{fname}`  ({size_kb:.1f} KB)",
                expanded=False,
            ):
                try:
                    content = read_workdir_file(project_id, fname)
                except Exception as e:  # noqa: BLE001
                    st.error(
                        f"Failed to read `{fname}`: "
                        f"`{type(e).__name__}: {e}`",
                    )
                    continue
                # HCL syntax highlighting via st.code's language hint.
                # Streamlit's HCL highlighter is rough but adequate.
                st.code(content, language="hcl")
                # Download button: per-file. Streamlit handles MIME +
                # file-name correctly for text/plain content.
                st.download_button(
                    label=f"📥 Download {fname}",
                    data=content,
                    file_name=fname,
                    mime="text/plain",
                    key=f"dl_{fname}",
                )

if not import_button:
    st.stop()

# --- Live import path ----------------------------------------------------

if _lock is not None:
    # Defensive (button was disabled but UI race could fire it)
    st.warning("Import already in progress; ignoring click.", icon="⚠️")
    st.stop()

# Acquire lock immediately so refresh-during-run shows "in progress"
st.session_state[_SS_RUN_LOCK] = {
    "start_ts": _time.time(),
    "project_id": project_id,
    "selected_count": len(selected_indices),
}

from app.middleware import workdir_context  # noqa: E402
from importer.run import run_workflow  # noqa: E402

started = time.monotonic()
try:
    with st.spinner(
        f"Importing {len(selected_indices)} resource(s) from "
        f"'{project_id}' … (LLM generation runs ~10-30s per resource)"
    ):
        with workdir_context(project_id) as workdir:
            result = run_workflow(
                project_id=project_id,
                selected_indices=selected_indices,
            )
except Exception as e:  # noqa: BLE001
    st.session_state.pop(_SS_RUN_LOCK, None)
    render_error(e, context=f"importing {len(selected_indices)} resources")
    st.stop()

# Clean exit: clear lock, cache result, refresh
st.session_state.pop(_SS_RUN_LOCK, None)
duration = time.monotonic() - started
result_dict = result.as_fields()
result_dict["duration_s"] = round(duration, 2)
st.session_state[_SS_LAST_RESULT] = result_dict

# Re-render to show the result card via the "last_result" branch above.
st.rerun()
