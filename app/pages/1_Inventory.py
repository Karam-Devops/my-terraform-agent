# app/pages/1_Inventory.py
"""Inventory page (PUI-1 + PUI-1B per-resource picker).

NOTE: file was named ``1_Importer.py`` until PUI-1B v3.6 -- renamed to
"Inventory" so the sidebar nav matches Firefly's vocabulary. The
underlying engine is still the importer (``importer.run.run_workflow``,
``importer.inventory.inventory``); only the UX label changed.

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
from app.ui.theme import apply_theme_polish


# Page chrome
# PUI-1B v3.6 RENAME: "Importer" -> "Inventory" to match Firefly's
# vocabulary. The underlying engine is still the importer (run_workflow,
# inventory.inventory) -- this is a UX-only relabel. We picked
# "Inventory" because that's the operator's mental model: "show me
# everything I own in this project, then let me codify what I want."
st.set_page_config(
    page_title="mtagent · Inventory",
    page_icon="📦",
    layout="wide",
)

# PUI-1B v3.4: Firefly-inspired theme polish (CSS injection).
apply_theme_polish()

project_id = render_sidebar()

st.title("📦 Inventory")
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


# --- PUI-1C v2 (2026-04-29 smoke 4 fix): Danger zone ALWAYS visible ----
# Pre-fix, the Danger Zone was rendered deep inside Stage B (after the
# data_editor + result card + files expander), so an operator who
# hadn't yet clicked Discover -- the very state where they most likely
# want to reset a stale workdir -- would never see the button (page
# st.stop()'d before reaching it).
#
# Now rendered RIGHT AFTER Stage A's Discover button, BEFORE the
# discovery-required early-stop. Self-contained tf_files fetch (uses
# the same _SS_TF_FILES cache key that Stage B uses, so it's free if
# Stage B already populated; otherwise this is the first fetch).
def _render_danger_zone() -> None:
    """Inline Reset-workdir UI, callable from any page state."""
    _ss_tf = f"_importer_tf_files_{project_id}"
    _ss_tf_err = f"_importer_tf_files_error_{project_id}"
    if _ss_tf not in st.session_state:
        try:
            from common.storage import list_workdir_tf_files  # noqa: E402
            st.session_state[_ss_tf] = list_workdir_tf_files(project_id)
            st.session_state[_ss_tf_err] = None
        except Exception as _e:  # noqa: BLE001
            st.session_state[_ss_tf] = []
            st.session_state[_ss_tf_err] = (
                f"{type(_e).__name__}: {_e}"[:200]
            )
    _files = st.session_state.get(_ss_tf, [])

    with st.expander("⚠️ Danger zone", expanded=False):
        st.markdown(
            f"### Reset workdir for `{project_id}`\n\n"
            f"This will permanently delete:\n"
            f"- All Terraform files in GCS at "
            f"`gs://mtagent-state-dev/tenants/default/projects/"
            f"{project_id}/`\n"
            f"- All quarantined files\n"
            f"- All terraform state "
            f"(`terraform-state/default.tfstate`)\n"
            f"- The local Streamlit cache for this project\n\n"
            f"It will **NOT** delete:\n"
            f"- The actual GCP resources in the project (those keep "
            f"running; only the IaC tracking is reset)\n"
            f"- Other projects' workdirs\n\n"
            f"_Use this when you want to start fresh on this project — "
            f"e.g., re-test the import flow from scratch, recover from "
            f"a corrupted state, or hand the project back to a customer "
            f"with no traces._"
        )
        _n_imp = sum(1 for f in _files if f["status"] == "imported")
        _n_quar = sum(
            1 for f in _files if f["status"] == "needs_attention"
        )
        if _n_imp == 0 and _n_quar == 0:
            st.caption(
                "Workdir is already empty in GCS — Reset will clear "
                "the local Streamlit cache only."
            )
        else:
            st.caption(
                f"Currently in workdir: **{_n_imp} imported file(s)**, "
                f"**{_n_quar} quarantined file(s)** "
                f"(plus terraform state and provider caches, "
                f"not listed)."
            )
        typed_confirm = st.text_input(
            f"Type the project ID to confirm: `{project_id}`",
            value="",
            key="reset_workdir_confirm",
            placeholder=project_id,
        )
        confirm_match = typed_confirm.strip() == project_id
        reset_btn_disabled = (
            (not confirm_match) or (_lock is not None)
        )
        reset_help = (
            "Type the project ID exactly to enable this button."
            if not confirm_match
            else "Import in progress; wait for it to complete."
            if _lock is not None
            else "Wipes GCS + local cache + session state. "
                 "Not reversible."
        )
        if st.button(
            "🗑️ Reset workdir",
            type="primary",
            disabled=reset_btn_disabled,
            key="reset_workdir_btn",
            help=reset_help,
        ):
            from common.storage import reset_workdir
            from app.middleware import bust_workdir_cache
            try:
                with st.spinner("🔄 Wiping GCS prefix..."):
                    gcs_result = reset_workdir(project_id)
                with st.spinner("🔄 Clearing local /tmp cache..."):
                    cache_result = bust_workdir_cache(project_id)
                # Clear all this-project session state.
                for _k in (
                    _SS_DISCOVERED, _ss_tf, _ss_tf_err, _SS_LAST_RESULT,
                ):
                    st.session_state.pop(_k, None)
                total_deleted = (
                    gcs_result["deleted_blobs"]
                    + gcs_result["deleted_versions"]
                )
                if total_deleted == 0 and not cache_result["cache_hit"]:
                    st.info(
                        f"Nothing to reset — workdir for "
                        f"`{project_id}` was already empty.",
                        icon="ℹ️",
                    )
                else:
                    st.success(
                        f"✅ Workdir reset for `{project_id}`. Removed "
                        f"**{gcs_result['deleted_blobs']} live + "
                        f"{gcs_result['deleted_versions']} archived** "
                        f"GCS object(s). Local cache: "
                        f"{'cleared' if cache_result['cache_hit'] else 'was empty'}. "
                        f"Re-discover above to start fresh.",
                        icon="✅",
                    )
            except Exception as _e:  # noqa: BLE001
                render_error(
                    _e, context=f"resetting workdir for {project_id}"
                )


# Always render Danger Zone -- regardless of discovery state. Operator
# may want to reset BEFORE running their first Discover (e.g., handing
# the project back to a customer, recovering from a wedged state).
_render_danger_zone()


if not discovered:
    st.info(
        "Click **Discover resources** above to see what's importable in "
        "this project. The list is cached in this session until you "
        "Re-discover or Clear.",
        icon="ℹ️",
    )
    st.stop()

# --- PUI-1F: compute per-row import status ----------------------------
# Cross-reference the discovered resources against what's already
# persisted in GCS (.tf at top-level vs in _quarantine/), so the picker
# grid can show a Status column AND the engine guard's source-of-truth
# is mirrored visually for the operator.
#
# Source of truth: _status.expected_tf_filename(tf_type, asset_type,
# cloud_name) -- same helper the engine's run_workflow uses to build
# its skip-set. Mirroring guarantees no UI/engine drift.
from importer._status import expected_tf_filename, classify_status

# Pull the persisted-files list from GCS (cached in session_state to
# avoid a per-rerun fetch). The "All generated files" expander below
# uses the same key, so opening it after picking is also free.
#
# PUI-1F v2 fix (2026-04-29 smoke): the previous version silently
# fell back to an empty list on GCS errors. Symptom: every row
# showed "Not imported" even when the workdir was full -- operator
# picked them in good faith, engine guard correctly skipped (since
# the .tf files DID exist), result card showed confusing "0 imported
# / 8 skipped". Surfacing the failure as a visible warning so the
# operator knows the Status column may be unreliable.
_SS_TF_FILES = f"_importer_tf_files_{project_id}"
_SS_TF_FILES_ERROR = f"_importer_tf_files_error_{project_id}"
if _SS_TF_FILES not in st.session_state:
    try:
        from common.storage import list_workdir_tf_files  # noqa: E402
        st.session_state[_SS_TF_FILES] = list_workdir_tf_files(project_id)
        st.session_state[_SS_TF_FILES_ERROR] = None
    except Exception as e:  # noqa: BLE001 -- captured + surfaced below
        st.session_state[_SS_TF_FILES] = []
        st.session_state[_SS_TF_FILES_ERROR] = (
            f"{type(e).__name__}: {e}"[:200]
        )

_tf_files = st.session_state.get(_SS_TF_FILES, [])
_tf_files_err = st.session_state.get(_SS_TF_FILES_ERROR)
if _tf_files_err:
    st.warning(
        f"⚠ Couldn't read the imported-file list from GCS — the "
        f"**Status column below may show 'Not imported' for resources "
        f"that ARE actually imported**. Re-discover or refresh the "
        f"page to retry. Underlying error: `{_tf_files_err}`",
        icon="⚠️",
    )
_imported_set = {f["name"] for f in _tf_files if f["status"] == "imported"}
_quarantined_set = {f["name"] for f in _tf_files if f["status"] == "needs_attention"}

# Pre-compute per-row metadata. For each discovered resource we derive:
#   * expected_filename (None if cloud_name is empty -- shouldn't happen
#     in practice but defensive)
#   * status            ("imported" / "needs_attention" / "none")
# Stored alongside the row dict so the table builder + selection
# resolver both have access without re-deriving.
_row_status: list[str] = []
_row_filenames: list[str] = []
for r in discovered:
    fname = expected_tf_filename(
        r["tfType"], r["assetType"], r["cloud_name"],
    )
    status = classify_status(
        fname,
        imported_set=_imported_set,
        quarantined_set=_quarantined_set,
    )
    _row_filenames.append(fname or "")
    _row_status.append(status)


# --- Stage B: Pick + Import ---------------------------------------------

st.markdown("---")

# Top-line counters (Firefly-style "Codified vs Un-codified" call-out
# above the picker). Operator sees at a glance how much of the project
# is already managed -- the demo story we want to lead with.
_n_imported = sum(1 for s in _row_status if s == "imported")
_n_needs_attn = sum(1 for s in _row_status if s == "needs_attention")
_n_uncodified = len(discovered) - _n_imported - _n_needs_attn

st.markdown(
    f"### Step 2 — Pick resources to import "
    f"({len(discovered)} discovered)"
)
m_uncod, m_cod, m_attn = st.columns(3)
m_uncod.metric("⚪ Not imported", _n_uncodified)
m_cod.metric("✅ Imported", _n_imported)
m_attn.metric("⚠️ Needs review", _n_needs_attn)
st.caption(
    "Selection defaults to NONE so a fat-finger Run never fires LLM "
    "cost. Use the column header checkbox to select all visible rows. "
    "Already-imported rows are hidden by default — uncheck the filter "
    "below to re-codify (LLM call + HCL overwrite)."
)

# Build a DataFrame-like list of dicts for st.data_editor's checkbox grid.
# Streamlit's data_editor works well with a dict-of-lists OR a list-of-dicts.
# For 80-row scale a list-of-dicts is fine and avoids a pandas dep here.
import pandas as pd  # already in requirements via streamlit

# Status-pill mapping for the Status column. Streamlit's data_editor
# can't render HTML inline (security model), so we use an emoji prefix
# + plain label that reads cleanly even without rich rendering.
_STATUS_LABEL = {
    "imported": "✅ Imported",
    "needs_attention": "⚠️ Needs review",
    "none": "⚪ Not imported",
}

# Build the editable table. The "Select" column is the only editable one;
# everything else is read-only.
table_rows = []
for idx, r in enumerate(discovered):
    table_rows.append({
        "Select": False,
        "#": idx + 1,  # 1-indexed to match CLI menu numbering
        "Status": _STATUS_LABEL[_row_status[idx]],
        "Name": r["displayName"] or r["cloud_name"],
        "Type": r["tfType"],  # show the friendly tf_type, not the URN
        "Location": r["location"],
    })
df = pd.DataFrame(table_rows)
# Carry status into the DataFrame so we can filter on it without
# re-aligning the indexer. Hidden from the rendered table via column
# config below.
df["_status_raw"] = _row_status

# Filter row: type multiselect + "Hide already imported" toggle (PUI-1F).
# Layout: 2 cols (filter + toggle) on top + visible-count metric on right.
all_tf_types = sorted({r["tfType"] for r in discovered})
filter_col, hide_col, count_col = st.columns([2, 1.2, 1])
with filter_col:
    type_filter = st.multiselect(
        "Filter by type",
        options=all_tf_types,
        default=[],
        placeholder="Show all types",
        key="type_filter",
    )
with hide_col:
    # PUI-1F: "Hide already imported" defaults to True (on) -- the
    # Firefly default. Operators rarely need to re-codify; making it
    # one extra click instead of the default keeps the picker focused
    # on un-codified work and makes re-codify a deliberate action.
    hide_imported = st.checkbox(
        "Hide already imported",
        value=True,
        key="hide_imported_toggle",
        help=(
            "When ON (default), already-imported resources are hidden "
            "from the picker -- can't be re-selected. Uncheck to expose "
            "them; selecting an imported row will re-fire the LLM and "
            "OVERWRITE the existing HCL on Run import."
        ),
    )
if type_filter:
    df = df[df["Type"].isin(type_filter)]
if hide_imported:
    df = df[df["_status_raw"] != "imported"]
with count_col:
    st.metric("Visible", len(df))

# Render the checkbox grid via data_editor.
#
# PUI-1B v3 (Option C, hover-tooltip variant): Streamlit's data_editor
# doesn't support per-cell tooltips, but column-header `help` text shows
# as a "?" icon on hover -- we use it on Name + Type to teach the
# disambiguation pattern (a VM and its auto-created boot disk often
# share the same Name; the Type column tells them apart).
edited_df = st.data_editor(
    df,
    column_config={
        "Select": st.column_config.CheckboxColumn(
            "Select", default=False, width="small",
        ),
        "#": st.column_config.NumberColumn(width="small"),
        "Status": st.column_config.TextColumn(
            "Status",
            help="Imported = top-level .tf exists for this resource "
                 "(no LLM call needed on Run import unless you re-"
                 "codify). Needs review = HCL exists but failed plan "
                 "verification (operator triage required). Not "
                 "imported = no .tf yet -- the LLM will generate one.",
            width="small",
        ),
        "Name": st.column_config.TextColumn(
            "Name",
            help="The resource's display name. NOTE: a Name may appear "
                 "under MULTIPLE Types -- e.g., a VM and its auto-"
                 "created boot disk both named `poc-vm`. Always check "
                 "the Type column to confirm which one you're picking.",
            width="large",
        ),
        "Type": st.column_config.TextColumn(
            "Type",
            help="The Terraform resource type. Same Name + different "
                 "Type = different resources (the disambiguator for "
                 "Name collisions).",
            width="medium",
        ),
        "Location": st.column_config.TextColumn(
            "Location",
            help="Zone (e.g. us-central1-a), region (us-central1), or "
                 "`global`. Empty `—` means the resource is project-"
                 "scoped without a location (e.g., Pub/Sub topics).",
            width="medium",
        ),
        # PUI-1F: hide the helper column we added for filtering. Streamlit's
        # data_editor renders every column in the DataFrame unless we
        # explicitly suppress it via the `hidden` flag.
        "_status_raw": None,
    },
    disabled=("#", "Status", "Name", "Type", "Location"),
    column_order=("Select", "#", "Status", "Name", "Type", "Location"),
    hide_index=True,
    use_container_width=True,
    key="resource_picker",
)

# The data_editor returns the edited DataFrame. Pull selected indices
# (1-indexed) from the rows where Select=True.
selected_indices = edited_df.loc[
    edited_df["Select"], "#"
].tolist()

# PUI-1F: detect whether any selected rows are already-imported. The
# data_editor's _status_raw column is preserved across the filter, so
# we can ask "is the operator's selection asking us to re-codify?"
# without re-running the status derivation.
selected_already_imported = []
for one_indexed in selected_indices:
    row_idx = one_indexed - 1
    if 0 <= row_idx < len(_row_status):
        if _row_status[row_idx] == "imported":
            selected_already_imported.append(
                {
                    "name": discovered[row_idx]["displayName"]
                            or discovered[row_idx]["cloud_name"],
                    "type": discovered[row_idx]["tfType"],
                }
            )
# Force-reimport is auto-enabled when the operator has explicitly
# unchecked "Hide already imported" AND picked one or more imported
# rows. We pass this through to run_workflow so the engine guard
# steps aside for those resources. Without the flag, the engine
# guard would silently skip them and the result card would show
# "skipped" for picks the operator clearly opted into.
force_reimport = bool(selected_already_imported)

if selected_already_imported:
    st.warning(
        f"⚠ Re-codify confirmation: **{len(selected_already_imported)} "
        f"already-imported resource(s)** are in your selection. Running "
        f"will fire the LLM and **overwrite** their existing HCL files. "
        f"To skip the re-codify, re-check 'Hide already imported' above "
        f"or untick those rows in the picker.",
        icon="⚠️",
    )

st.markdown("---")
import_col, info_col = st.columns([1, 2])
with import_col:
    # PUI-1B v3.2: button uses st.empty() placeholder so we can SWAP
    # it to a disabled "Running..." state IMMEDIATELY after click,
    # before the heavy imports + workflow start.
    #
    # Without the placeholder, the button is rendered FIRST in the
    # script rerun (with its primary-active text), then the lock is
    # set, then the workflow runs (blocking 10+ min). The visual
    # update to "Running..." doesn't happen until the workflow
    # completes -- so during the entire run the button still looks
    # clickable. Operators with a fast finger may double-click;
    # Streamlit's script-execution-lock prevents the second click
    # from doing anything but the visual is misleading.
    #
    # With the placeholder, we render the button into the slot, and
    # if the click fires (import_button=True) we IMMEDIATELY replace
    # the slot's content with a disabled "Running..." button. The
    # operator sees the button greyed out within milliseconds of click.
    import_btn_slot = st.empty()
    import_button = import_btn_slot.button(
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

    # PUI-1F Option C (2026-04-29): when the engine guard auto-skipped
    # everything (selected=N, imported=0, failed=0, auto_skipped=N),
    # the neutral "Imported 0 / Skipped N" metric grid reads as a
    # failure even though it's actually the engine correctly NOT
    # burning N LLM calls on already-imported resources. Replace the
    # metric grid with a positive success banner for that case --
    # "X resources already imported; nothing to do."
    _imp = last_result.get("imported", 0)
    _failed = last_result.get("failed", 0)
    _needs = last_result.get("needs_attention", 0)
    _skipped = last_result.get("skipped", 0)
    _auto_skipped = last_result.get("auto_skipped", 0)
    _auto_corrected = last_result.get("auto_corrected", 0)
    _all_auto_skipped = (
        _imp == 0 and _failed == 0 and _needs == 0
        and _auto_skipped > 0 and _auto_skipped == _skipped
    )

    if _all_auto_skipped:
        # Pure-success "everything already imported" path. Single
        # green banner with the duration + count. No metric grid
        # (it would just show "0 / 0 / N / 0" which is misleading).
        st.success(
            f"✅ All {_auto_skipped} selected resource(s) were "
            f"**already imported** — engine skipped the LLM call to "
            f"preserve existing HCL. Completed in "
            f"{last_result.get('duration_s', 0):.1f}s. "
            f"To re-codify any of them, untick **Hide already "
            f"imported** above and re-pick.",
            icon="✅",
        )
    else:
        # Mixed or genuinely-imported run -- show the standard
        # metric grid. When auto_skipped > 0 in a mixed run, surface
        # it as a small caption beneath so the operator knows the
        # `skipped` total includes the engine guard's contribution.
        st.success(
            f"✅ Last run completed in "
            f"{last_result.get('duration_s', 0):.1f}s",
            icon="✅",
        )
        m1, m2, m3, m4 = st.columns(4)
        m1.metric("Imported", _imp)
        m2.metric("Needs attention", _needs)
        m3.metric("Skipped", _skipped)
        m4.metric("Failed", _failed)
        if _auto_skipped > 0:
            st.caption(
                f"_Of the {_skipped} skipped, {_auto_skipped} were "
                f"auto-skipped (already imported — engine guard "
                f"saved the LLM call). The remaining "
                f"{_skipped - _auto_skipped} had no Terraform mapping "
                f"for their asset type._"
            )
        # PUI-1F v3.3 Fix 1: surface Auto-Correction Loop activity.
        # When auto_corrected > 0, some imports needed at least one
        # LLM retry to pass plan-verify. This is a SUCCESS signal --
        # the system silently fixed LLM hallucinations that would
        # otherwise have quarantined. Showing it builds trust ("I
        # see the system fixing things automatically").
        if _auto_corrected > 0:
            st.caption(
                f"_Of the {_imp} imported, **{_auto_corrected} needed "
                f"the Auto-Correction Loop** to fix a transient LLM "
                f"mistake (1–{_auto_corrected * 5} extra retry "
                f"call(s)). Cloud Logging has per-attempt detail "
                f"(filter on `event=auto_correction_attempt_*`)._"
            )
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
    "📄 All generated Terraform files for this project",
    expanded=False,
):
    # PUI-1B v3.5 (count-reconciliation note): operators noticed the
    # totals here can differ from the "Last import result" card above.
    # That's by design but easy to misread, so we name the relationship
    # explicitly:
    #
    #   * The result card counts ONLY the resources picked in the most
    #     recent click (engine bookkeeping; reset on every run).
    #   * The list below shows EVERY .tf file persisted in GCS for this
    #     project across ALL prior runs (filesystem view; cumulative).
    #
    # Example: a project with 3 buckets imported last week + 11 GCE
    # resources today shows "11 imported" on the card and "14 imported"
    # in this section. Both are correct -- they answer different
    # questions ("what did this run do?" vs "what does my project look
    # like end-to-end?").
    st.caption(
        "Cumulative view: every Terraform file persisted for this "
        "project, across all prior import runs. The **Last import "
        "result** card above only counts the resources picked in the "
        "most recent click — that's why the totals differ."
    )
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
        # PUI-1B v3.3 (Firefly-style status grouping):
        # Split files by status -> two clearly-labelled subsections
        # with color-coded headers + badges. Imported (green/✅) on top
        # because that's the celebration; needs_attention (orange/⚠️)
        # below where the operator focuses to triage.
        from common.storage import read_workdir_file  # noqa: E402

        imported_files = [f for f in tf_files if f["status"] == "imported"]
        needs_attn_files = [
            f for f in tf_files if f["status"] == "needs_attention"
        ]

        # Top-line summary
        c_ok, c_warn, c_total = st.columns(3)
        c_ok.metric("✅ Imported", len(imported_files))
        c_warn.metric("⚠️ Needs attention", len(needs_attn_files))
        c_total.metric("Total files", len(tf_files))

        # ----- Imported (green section) -----
        if imported_files:
            st.markdown(
                f"#### ✅ Successfully imported ({len(imported_files)})"
            )
            st.caption(
                "These resources imported into terraform state AND "
                "passed `terraform plan` verification. The HCL is "
                "production-ready."
            )
            for tf_file in imported_files:
                fname = tf_file["name"]
                size_kb = tf_file["size_bytes"] / 1024
                with st.expander(
                    f"✅  `{fname}`  ({size_kb:.1f} KB)",
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
                    st.code(content, language="hcl")
                    st.download_button(
                        label=f"📥 Download {fname}",
                        data=content,
                        file_name=fname,
                        mime="text/plain",
                        key=f"dl_{fname}",
                    )

        # ----- Needs attention (orange section) -----
        if needs_attn_files:
            st.markdown(
                f"#### ⚠️ Needs attention ({len(needs_attn_files)})"
            )
            # PUI-1B v3.5 (review-queue framing):
            # Earlier copy here led with "the LLM generated mutually-
            # exclusive fields" -- factually accurate for one common
            # case but unfairly framed every needs_attention row as a
            # codegen failure. Real-world drivers are broader: the
            # cloud snapshot may carry deprecated arguments, location-
            # locked enums, or schema mutex pairs the provider rejects
            # on plan even though the resource itself imported cleanly.
            #
            # Reframe as a NORMAL triage queue (Firefly's "review"
            # pattern) instead of an apology. The operator's job here
            # is the same -- read the provider error, edit or skip --
            # but the affordance reads as "expected workflow step,"
            # not "the system failed and you're cleaning up."
            st.info(
                "**Review queue.** Terraform state was imported "
                "successfully for these resources, but `terraform "
                "plan` flagged HCL-level differences that need a "
                "human decision — typically schema constraints "
                "(mutually-exclusive field pairs, deprecated "
                "arguments, or values the provider doesn't accept "
                "verbatim from the cloud snapshot).\n\n"
                "**Next step:** open each card below to see the exact "
                "provider error and the generated HCL. Edit the file "
                "inline, or leave it aside — the import is preserved "
                "either way.",
                icon="📝",
            )
            for tf_file in needs_attn_files:
                fname = tf_file["name"]
                size_kb = tf_file["size_bytes"] / 1024
                with st.expander(
                    f"⚠️  `{fname}`  ({size_kb:.1f} KB) — needs review",
                    expanded=False,
                ):
                    # Show the error preview FIRST -- operator's first
                    # question is "why did this fail?"
                    error_preview = tf_file.get("error_preview")
                    if error_preview:
                        st.error(
                            f"**Why it failed verification:**\n\n"
                            f"```\n{error_preview}\n```",
                            icon="🛑",
                        )
                    else:
                        st.error(
                            "Plan-verification failed (no error preview "
                            "available). Check the .quarantine.txt "
                            "sidecar in GCS for full details.",
                            icon="🛑",
                        )
                    # PUI-1F v3.2 (2026-04-29 smoke 5 cleanup): the
                    # "Show full quarantine details" expander shipped in
                    # v3.1 was redundant. Now that the error_preview
                    # skips the preamble + caps at 1500 chars, the
                    # preview shows the actual diagnostic content for
                    # essentially every quarantine. Operators flagged
                    # the duplicate cards as confusing in smoke 5
                    # ("why am I seeing 2 cards"). Removed.
                    #
                    # The read_quarantine_sidecar helper in
                    # common.storage stays available for future use
                    # (e.g., a "Download full sidecar" button) and for
                    # operator gcloud-debugging continuity.

                    # Then the HCL itself
                    try:
                        content = read_workdir_file(
                            project_id, fname, from_quarantine=True,
                        )
                    except Exception as e:  # noqa: BLE001
                        st.error(
                            f"Failed to read quarantined `{fname}`: "
                            f"`{type(e).__name__}: {e}`",
                        )
                        continue
                    st.markdown("**Generated HCL (review this):**")
                    st.code(content, language="hcl")
                    st.download_button(
                        label=f"📥 Download {fname}",
                        data=content,
                        file_name=fname,
                        mime="text/plain",
                        key=f"dl_quarantine_{fname}",
                    )

# NOTE: Danger Zone is rendered earlier in the page (right after Stage A's
# Discover button, BEFORE the discovery-required early-stop). See
# _render_danger_zone() above. This was moved out of the Stage B render
# path in the 2026-04-29 smoke 4 fix so operators can reset a workdir
# without first having to click Discover.

if not import_button:
    st.stop()

# --- Live import path ----------------------------------------------------

if _lock is not None:
    # Defensive (button was disabled but UI race could fire it)
    st.warning("Import already in progress; ignoring click.", icon="⚠️")
    st.stop()

# PUI-1B v3 follow-up: IMMEDIATE visual feedback on click.
#
# Operator feedback during smoke 2026-04-29: "After clicking Run Import
# it takes a lot of time before the UI shows the run was kicked off
# (makes you wanna click Run Import again)."
#
# Root cause of perceived lag: the click triggers a Streamlit script
# rerun that re-renders the WHOLE page (sidebar, picker grid with N=80
# rows, expanders) BEFORE reaching this import path. Heavy module
# imports (importer.run + dependencies) take another ~5-15s on cold
# container. THEN the spinner finally appears.
#
# Three-part fix (in order of perceptual impact):
#   1. Swap the button slot to a disabled "Running..." button NOW.
#      Operator sees button greyed out within milliseconds.
#   2. Browser toast notification.
#   3. In-page green banner.
# All three render BEFORE heavy imports + spinner.

# (1) Disable + relabel the button via the placeholder. Streamlit
#     evaluates the placeholder slot's most recent content; this
#     replaces the primary "Run import" with a secondary disabled
#     "Running..." button at the same position. Visually instant.
import_btn_slot.button(
    f"⚡ Running ({len(selected_indices)} resource(s))…",
    type="secondary",
    disabled=True,
    key="run_import_btn_disabled_swap",
    use_container_width=True,
)

# (2) Browser toast -- lightweight popup, doesn't shift layout.
st.toast(
    f"⚡ Starting import of {len(selected_indices)} resource(s)...",
    icon="🚀",
)

# (3) In-page green banner with project context.
st.success(
    f"🚀 Import started for **{project_id}** "
    f"({len(selected_indices)} resource(s) selected). "
    f"Loading engine modules...",
    icon="🚀",
)

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
                # PUI-1F: pass through the operator's explicit re-codify
                # opt-in (only true when they unchecked "Hide already
                # imported" AND selected at least one imported row).
                force_reimport=force_reimport,
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

# PUI-1F: bust the persisted-files cache so the picker grid's Status
# column re-fetches and reflects the just-imported resources on the
# next render. Without this, an operator who imports 3 buckets and
# then revisits the picker still sees them as "Not imported" until
# they manually click the Refresh list button -- exactly the kind of
# stale UI that makes the engine guard feel inconsistent.
st.session_state.pop(_SS_TF_FILES, None)

# Re-render to show the result card via the "last_result" branch above.
st.rerun()
