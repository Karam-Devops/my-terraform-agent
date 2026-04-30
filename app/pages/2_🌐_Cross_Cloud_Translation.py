# app/pages/2_Cross_Cloud_Translation.py
"""Translator page (PUI-3).

Three-stage workflow mirroring the Inventory page's UX:

  Stage A — DISCOVER (cheap, no LLM cost):
    Operator picks the project (sidebar), the page lists every
    importer-output `.tf` file via common.storage.list_workdir_tf_files
    (status="imported" only -- quarantined files are never sent to
    the translator, the operator should fix them in Inventory first).

  Stage B — PICK + TRANSLATE:
    Operator checks source files, picks target cloud (AWS or Azure
    via radio), clicks "Translate". Each LLM call costs ~$0.20-0.50
    and takes 20-60s (Phase 1 blueprint extract + Phase 2 generate
    + up to 3 validate-feedback retries). PUI-1F's lifecycle and
    safety patterns carry over: run lock, button visual swap, danger
    zone reset.

  Stage C — RESULTS:
    TranslationResult metric grid + per-file outcomes + expander
    showing the generated `aws_*.tf` / `azurerm_*.tf` files (read
    from GCS via list_translated_files). Status pills:
        translated (green), needs_attention (orange), failed (red).

Why a separate page (not a tab on Inventory): Streamlit's pages/
nav makes per-engine state isolation easy + each page can be
deep-linked for demo workflows.

Engine wiring:
  * translator.run.run_translation_batch(target_cloud, source_paths,
    tenant_id, project_id) is the headless entry point. Already CLI/
    SaaS parity-audited (2026-04-29) -- no defense gaps.
  * common.storage.list_translated_files / read_translated_file /
    reset_translated wrap GCS for the UI surface.
  * app.middleware.workdir_context handles per-request hydrate +
    persist (translated/<target>/*.tf gets uploaded on exit).

Theme: same Firefly DARK polish as Inventory via apply_theme_polish().
Mint accent (#00C4A7) on the primary "Translate" button; red on the
Danger Zone reset; status pills follow the Material palette
documented in app/ui/theme.py.
"""

import os
import time

import streamlit as st

from app.ui.sidebar import render_sidebar
from app.ui.error_surface import render_error
from app.ui.theme import apply_theme_polish

# PUI-3a (2026-04-29): read the allowlist from translator.config so the
# UI's target-cloud radio options match whatever the deployed env
# permits. Round-1 SaaS sets TRANSLATOR_TARGETS_ALLOWED=aws so the
# customer-facing UI is AWS-only; internal/test runs leave the env
# var unset and get both AWS + Azure. This avoids a UI-vs-engine
# mismatch (e.g. UI offers Azure but engine rejects it).
#
# We further filter to {aws, azure} -- TRANSLATOR_TARGETS_ALLOWED
# may include "yaml" (the translator's blueprint dump format), but
# that's an internal debug surface, never a customer-facing target.
from translator.config import TRANSLATOR_TARGETS_ALLOWED  # noqa: E402

_UI_TARGETS_SUPPORTED = ("aws", "azure")
_UI_TARGETS = [
    t for t in _UI_TARGETS_SUPPORTED if t in TRANSLATOR_TARGETS_ALLOWED
]
# Defensive fallback: if the env var is misconfigured to allow neither
# AWS nor Azure (e.g. set to "yaml" only), default to AWS so the page
# is still usable. Logged elsewhere via translator.config import.
if not _UI_TARGETS:
    _UI_TARGETS = ["aws"]


# Page chrome
st.set_page_config(
    page_title="mtagent · Cross-Cloud Translation",
    page_icon="🌐",
    layout="wide",
)

# Same Firefly DARK polish + CSS overrides as Inventory.
apply_theme_polish()

project_id = render_sidebar()

st.title("🌐 Cross-Cloud Translation")
st.caption(
    "Translate imported GCP `google_*` HCL into AWS or Azure equivalents. "
    "Each translation runs through Phase 1 (blueprint extract) + "
    "Phase 2 (target-cloud HCL gen) with up to 3 validate-feedback "
    "retries."
)

if not project_id:
    st.warning("Pick a project in the sidebar to get started.", icon="⚠️")
    st.stop()

st.markdown(f"**Project:** `{project_id}`")

# Session-state keys -- all prefixed with `_translator_` to avoid
# colliding with the Inventory page's `_importer_*` keys.
_SS_RUN_LOCK = "_translator_run_lock"
_SS_LAST_RESULT = f"_translator_last_result_{project_id}"
_SS_TRANSLATED_AWS = f"_translator_translated_aws_{project_id}"
_SS_TRANSLATED_AZURE = f"_translator_translated_azure_{project_id}"
_SS_SOURCE_FILES = f"_translator_source_files_{project_id}"
_SS_SOURCE_FILES_ERROR = f"_translator_source_files_err_{project_id}"


# --- Tier-A run lock ----------------------------------------------------
# Mirrors the Inventory page's lock. 10-min hard timeout +
# PUI-3b auto-recover for the websocket-drop case where the engine
# completed but the browser never received st.rerun()'s message.
import time as _time

_RUN_TIMEOUT_S = 600
_lock = st.session_state.get(_SS_RUN_LOCK)
_last_result_for_recover = st.session_state.get(_SS_LAST_RESULT)

if _lock is not None:
    _elapsed = _time.time() - _lock.get("start_ts", 0)
    # Hard timeout: 10-min absolute ceiling. Catches genuine hangs
    # (engine deadlocked, network failure mid-call).
    if _elapsed > _RUN_TIMEOUT_S:
        st.session_state.pop(_SS_RUN_LOCK, None)
        _lock = None

    # PUI-3b auto-recover (2026-04-30): the lock might be stale because
    # the engine completed + result was cached, but the browser missed
    # the st.rerun() message (Cloud Run idle-WebSocket timeout drops
    # the connection during long blocking calls). Detection: a result
    # was cached AFTER this lock acquired -> the engine ran to
    # completion -> it's safe to clear the stale lock and render the
    # success card.
    elif (
        _last_result_for_recover is not None
        and _last_result_for_recover.get("_cached_at", 0)
        > _lock.get("start_ts", 0)
    ):
        _log_msg = (
            f"PUI-3b auto-recover: clearing stale run-lock "
            f"({int(_elapsed)}s old; result was cached "
            f"{int(_time.time() - _last_result_for_recover['_cached_at'])}s "
            f"ago). Engine completed but websocket likely dropped."
        )
        # Print to stdout so Cloud Logging captures the auto-recover
        # event (no _log here -- we're in a UI module).
        print(_log_msg)
        st.session_state.pop(_SS_RUN_LOCK, None)
        _lock = None


# --- Source files fetch (shared with Inventory's GCS scan) -------------
# We list .tf files at the project's GCS top-level (= imported,
# importer-output) and filter to status="imported" -- quarantined
# files are not translatable until the operator fixes them in
# Inventory. Cached in session_state so the picker grid + Danger
# Zone pre-flight share one round-trip.
if _SS_SOURCE_FILES not in st.session_state:
    try:
        from common.storage import list_workdir_tf_files  # noqa: E402
        all_files = list_workdir_tf_files(project_id)
        st.session_state[_SS_SOURCE_FILES] = [
            f for f in all_files if f["status"] == "imported"
        ]
        st.session_state[_SS_SOURCE_FILES_ERROR] = None
    except Exception as _e:  # noqa: BLE001
        st.session_state[_SS_SOURCE_FILES] = []
        st.session_state[_SS_SOURCE_FILES_ERROR] = (
            f"{type(_e).__name__}: {_e}"[:200]
        )

source_files = st.session_state.get(_SS_SOURCE_FILES, [])
source_err = st.session_state.get(_SS_SOURCE_FILES_ERROR)
if source_err:
    st.warning(
        f"⚠ Couldn't read the imported-file list from GCS — the "
        f"picker below may be empty. Underlying error: "
        f"`{source_err}`",
        icon="⚠️",
    )


# --- Danger Zone (always visible, mirrors Inventory pattern) -----------
# Per-target-cloud reset: lets the operator wipe AWS-only or
# Azure-only outputs. Imported source files at project root are
# untouched.
def _render_danger_zone() -> None:
    """Inline reset UI for translator outputs. Per-target-cloud."""
    with st.expander("⚠️ Danger zone", expanded=False):
        st.markdown(
            f"### Reset translated files for `{project_id}`\n\n"
            f"This will permanently delete the operator's previous "
            f"translation outputs at:\n"
            f"- `gs://mtagent-state-dev/tenants/default/projects/"
            f"{project_id}/translated/<aws|azure>/`\n\n"
            f"It will **NOT** delete:\n"
            f"- Your imported source `.tf` files (at project root)\n"
            f"- Translations for the OTHER target cloud (each is "
            f"reset independently)\n\n"
            f"_Use when you want to redo a translation cleanly. Pick "
            f"the target cloud below, type the project ID to confirm, "
            f"then click Reset._"
        )

        # Pre-flight counts: how many translated files exist per target?
        try:
            from common.storage import list_translated_files  # noqa: E402
            aws_files = list_translated_files(project_id, "aws")
            azure_files = list_translated_files(project_id, "azure")
        except Exception:  # noqa: BLE001 -- defensive; shouldn't block UI
            aws_files = []
            azure_files = []

        c_aws, c_az = st.columns(2)
        c_aws.caption(
            f"AWS outputs: **{len(aws_files)} file(s)**"
            if aws_files else "AWS outputs: empty"
        )
        c_az.caption(
            f"Azure outputs: **{len(azure_files)} file(s)**"
            if azure_files else "Azure outputs: empty"
        )

        # PUI-3a: respect the allowlist for reset-target options too.
        # Single-target deploy hides the radio and pins to that target.
        if len(_UI_TARGETS) == 1:
            reset_target = _UI_TARGETS[0]
            st.caption(
                f"Reset target: **{reset_target.upper()}** "
                f"(only target enabled in this deploy)"
            )
        else:
            reset_target = st.radio(
                "Target cloud to reset",
                options=_UI_TARGETS,
                format_func=lambda v: v.upper(),
                horizontal=True,
                key="dz_reset_target",
            )
        typed_confirm = st.text_input(
            f"Type the project ID to confirm: `{project_id}`",
            value="",
            key="dz_reset_confirm",
            placeholder=project_id,
        )
        confirm_match = typed_confirm.strip() == project_id
        reset_btn_disabled = (
            (not confirm_match) or (_lock is not None)
        )
        reset_help = (
            "Type the project ID exactly to enable this button."
            if not confirm_match
            else "Translation in progress; wait for it to complete."
            if _lock is not None
            else f"Wipes {reset_target.upper()} outputs only. Not reversible."
        )
        if st.button(
            f"🗑️ Reset {reset_target.upper()} translations",
            type="primary",
            disabled=reset_btn_disabled,
            key="dz_reset_btn",
            help=reset_help,
        ):
            from common.storage import reset_translated  # noqa: E402
            try:
                with st.spinner(
                    f"🔄 Wiping {reset_target.upper()} outputs..."
                ):
                    result = reset_translated(project_id, reset_target)
                # Bust caches for this target.
                if reset_target == "aws":
                    st.session_state.pop(_SS_TRANSLATED_AWS, None)
                else:
                    st.session_state.pop(_SS_TRANSLATED_AZURE, None)
                st.session_state.pop(_SS_LAST_RESULT, None)
                total = (
                    result["deleted_blobs"]
                    + result["deleted_versions"]
                )
                if total == 0:
                    st.info(
                        f"Nothing to reset — `{reset_target.upper()}` "
                        f"outputs were already empty.",
                        icon="ℹ️",
                    )
                else:
                    st.success(
                        f"✅ Reset {reset_target.upper()} translations "
                        f"for `{project_id}`. Removed "
                        f"**{result['deleted_blobs']} live + "
                        f"{result['deleted_versions']} archived** "
                        f"GCS object(s).",
                        icon="✅",
                    )
            except Exception as _e:  # noqa: BLE001
                render_error(
                    _e,
                    context=(
                        f"resetting {reset_target} translations "
                        f"for {project_id}"
                    ),
                )


_render_danger_zone()


# --- Stage 1: Target cloud (BEFORE picker, so Status column can ----
# compute against the active target).
st.markdown("---")

if not source_files:
    st.info(
        "No imported source files found for this project. Run the "
        "**Inventory** page's import flow first to generate "
        "translatable `.tf` files.",
        icon="ℹ️",
    )
    st.stop()

st.markdown("### Step 1 — Target cloud")
# PUI-3a: build radio options from TRANSLATOR_TARGETS_ALLOWED. Single-
# target deploys hide the radio entirely and pin to that target.
if len(_UI_TARGETS) == 1:
    target_cloud = _UI_TARGETS[0]
    st.markdown(
        f"**Target cloud:** "
        f"{'🟧 AWS' if target_cloud == 'aws' else '🟦 Azure'}  "
        f"_({len(_UI_TARGETS) == 1 and 'only target enabled in this deploy' or ''})_"
    )
else:
    target_cloud = st.radio(
        "Translate to",
        options=_UI_TARGETS,
        format_func=lambda v: (
            "🟧 AWS" if v == "aws" else "🟦 Azure"
        ),
        horizontal=True,
        key="tx_target_cloud",
    )


# --- PUI-3c: compute per-row translation status against active target.
# Cross-references each source file against the persisted translated/
# <target>/ directory so the picker grid shows what's done vs not.
# Reuses the per-target session-state cache populated below by the
# "Generated translations" expanders -- one fetch covers both views.
def _expected_translated_name(source_filename: str, target: str) -> str:
    """Return the filename the translator would write for this source.

    Mirrors translator.run.resolve_output_path's basename derivation:
    drop ``google_`` from the source basename, prepend
    ``<target>_translated_``. Pure function; safe to call per-row in
    the picker render.

    Examples:
        google_storage_bucket_poc.tf, aws
            -> aws_translated_storage_bucket_poc.tf
        google_compute_disk_poc-disk.tf, azure
            -> azure_translated_compute_disk_poc-disk.tf
    """
    return f"{target}_translated_{source_filename.replace('google_', '')}"


_target_ss_key = (
    _SS_TRANSLATED_AWS if target_cloud == "aws"
    else _SS_TRANSLATED_AZURE
)
# Lazy fetch the translated-files list for the active target. Same
# cache key the lower expander uses, so this is free if either has
# already populated.
if _target_ss_key not in st.session_state:
    try:
        from common.storage import list_translated_files  # noqa: E402
        st.session_state[_target_ss_key] = list_translated_files(
            project_id, target_cloud,
        )
    except Exception:  # noqa: BLE001 -- soft-fail; status -> "none"
        st.session_state[_target_ss_key] = []

_translated_set = {
    f["name"] for f in st.session_state.get(_target_ss_key, [])
}

# Per-row status. Two values:
#   "translated" - target's expected output filename exists in GCS
#   "not_translated" - operator hasn't run translate for this file
# (No third "needs_attention" yet -- translator's failures land in
# the per-file FileOutcome of the LAST result, not in a separate
# quarantine directory like Inventory uses.)
_row_status: list[str] = []
for f in source_files:
    expected = _expected_translated_name(f["name"], target_cloud)
    _row_status.append(
        "translated" if expected in _translated_set else "not_translated"
    )

# Top-line counters above the picker (Firefly-style "Translated vs
# Not translated" call-out). Matches Inventory's metrics-row.
_n_translated = sum(1 for s in _row_status if s == "translated")
_n_pending = len(source_files) - _n_translated


st.markdown("---")
st.markdown(
    f"### Step 2 — Pick source files "
    f"({len(source_files)} available)"
)
m_pending, m_done = st.columns(2)
m_pending.metric("⚪ Not translated", _n_pending)
m_done.metric(f"✅ Translated to {target_cloud.upper()}", _n_translated)

st.caption(
    f"Status reflects translations to **{target_cloud.upper()}** only. "
    f"Switching target above re-evaluates the column. Already-"
    f"translated rows are hidden by default — re-translation overwrites "
    f"the existing output file (same as the importer's re-codify "
    f"behaviour)."
)

import pandas as pd  # already in requirements via streamlit

# Status pill labels (matches Inventory's _STATUS_LABEL convention --
# emoji prefix + plain text since data_editor doesn't render HTML).
_TX_STATUS_LABEL = {
    "translated": f"✅ Translated",
    "not_translated": "⚪ Not translated",
}

# Build the editable picker grid. Status column joins Inventory's
# pattern; Source file + Size + the helper #/Select columns mirror.
table_rows = []
for idx, f in enumerate(source_files):
    table_rows.append({
        "Select": False,
        "#": idx + 1,
        "Status": _TX_STATUS_LABEL[_row_status[idx]],
        "Source file": f["name"],
        "Size": f"{f['size_bytes'] / 1024:.1f} KB",
    })
df = pd.DataFrame(table_rows)
# Helper column for filtering -- hidden in the rendered picker via
# column_config below. Same trick as Inventory's _status_raw.
df["_status_raw"] = _row_status

# Filter by tf_type prefix (extracted from filename) so operators with
# many resources can narrow down. We sniff the prefix from the
# filename pattern <tf_type>_<hcl_name>.tf.
def _sniff_tf_type(filename: str) -> str:
    """Best-effort tf_type extraction; falls back to the bare basename."""
    # importer filename = <tf_type>_<hcl_name>.tf
    # tf_type always starts with 'google_'
    if filename.startswith("google_"):
        # Find the last underscore segment that looks like a tf_type
        # boundary -- imperfect but useful for the filter.
        # Heuristic: take everything up to last underscore.
        return filename.rsplit("_", 1)[0]
    return filename


type_options = sorted({_sniff_tf_type(r["Source file"]) for r in table_rows})
# 3-column filter row: type-multiselect + Hide-translated + visible-count.
filter_col, hide_col, count_col = st.columns([2, 1.2, 1])
with filter_col:
    type_filter = st.multiselect(
        "Filter by inferred type prefix",
        options=type_options,
        default=[],
        placeholder="Show all",
        key="tx_type_filter",
    )
with hide_col:
    # PUI-3c: same default-ON pattern as Inventory's "Hide already
    # imported" -- re-translate becomes a deliberate action (untick
    # the box to expose translated rows; selecting any triggers the
    # re-translate confirmation banner below).
    hide_translated = st.checkbox(
        "Hide already translated",
        value=True,
        key="tx_hide_translated",
        help=(
            f"When ON (default), files already translated to "
            f"**{target_cloud.upper()}** are hidden. Uncheck to "
            f"expose them; selecting any will overwrite the existing "
            f"output on Translate."
        ),
    )
if type_filter:
    df = df[df["Source file"].apply(
        lambda f: _sniff_tf_type(f) in type_filter
    )]
if hide_translated:
    df = df[df["_status_raw"] != "translated"]
with count_col:
    st.metric("Visible", len(df))

edited_df = st.data_editor(
    df,
    column_config={
        "Select": st.column_config.CheckboxColumn(
            "Select", default=False, width="small",
        ),
        "#": st.column_config.NumberColumn(width="small"),
        "Status": st.column_config.TextColumn(
            "Status",
            help=(
                f"Translated = `{target_cloud.upper()}` output exists "
                f"in GCS for this source. Not translated = no output "
                f"yet (the LLM will generate one on Translate). "
                f"Switching target above re-evaluates this column."
            ),
            width="small",
        ),
        "Source file": st.column_config.TextColumn(
            "Source file",
            help="Filename of the imported `.tf` to translate. The "
                 "translator extracts a YAML blueprint from it then "
                 "generates equivalent HCL for the target cloud.",
            width="large",
        ),
        "Size": st.column_config.TextColumn(
            "Size", width="small",
        ),
        # Hide the helper column we used for filtering. Streamlit
        # renders every DataFrame column unless explicitly suppressed.
        "_status_raw": None,
    },
    disabled=("#", "Status", "Source file", "Size"),
    column_order=("Select", "#", "Status", "Source file", "Size"),
    hide_index=True,
    use_container_width=True,
    key="tx_resource_picker",
)

# Picked indices -> picked filenames.
picked_indices = edited_df.loc[
    edited_df["Select"], "#"
].tolist()
picked_filenames = [
    source_files[i - 1]["name"] for i in picked_indices
    if 0 < i <= len(source_files)
]

# PUI-3c: detect re-translate intent. If any picked row is already
# translated for the active target, show a confirmation banner
# (matches Inventory's "Re-codify confirmation" pattern). Translation
# always overwrites the output file, so this is awareness-only --
# the engine doesn't need a force_retranslate flag the way the
# importer does.
selected_already_translated = []
for one_indexed in picked_indices:
    row_idx = one_indexed - 1
    if 0 <= row_idx < len(_row_status):
        if _row_status[row_idx] == "translated":
            selected_already_translated.append(
                source_files[row_idx]["name"]
            )

if selected_already_translated:
    st.warning(
        f"⚠ Re-translate confirmation: "
        f"**{len(selected_already_translated)} already-translated "
        f"file(s)** are in your selection. Running will fire the LLM "
        f"again and **overwrite** the existing "
        f"`{target_cloud.upper()}` output(s). Re-check 'Hide already "
        f"translated' above to skip them.",
        icon="⚠️",
    )


# --- Stage 3: Translate button ----------------------------------------

st.markdown("---")
st.markdown("### Step 3 — Run translate")

_, button_col = st.columns([0.0001, 3])
with button_col:
    # Same st.empty() placeholder pattern Inventory uses for
    # immediate visual feedback on click.
    translate_btn_slot = st.empty()
    translate_button = translate_btn_slot.button(
        f"▶ Translate ({len(picked_filenames)} selected) → "
        f"{target_cloud.upper()}"
        if not _lock
        else f"Translating ({int(_time.time() - _lock['start_ts'])}s)…",
        type="primary",
        disabled=(len(picked_filenames) == 0 or _lock is not None),
        key="tx_translate_btn",
        use_container_width=True,
    )

if _lock is not None:
    st.warning(
        f"⏳ Translation in progress for **{_lock.get('project_id')}** "
        f"({_lock.get('target_cloud', '?').upper()}); started "
        f"{int(_time.time() - _lock['start_ts'])}s ago. Wait or "
        f"refresh after {_RUN_TIMEOUT_S // 60} min if stuck.",
        icon="⏳",
    )
elif len(picked_filenames) == 0:
    st.caption(
        "Select at least one source file above to enable Translate."
    )
else:
    # Cost / time estimate. Each file = Phase 1 LLM (~10-20s) +
    # Phase 2 LLM (~10-30s) + up to 3 validate-feedback retries
    # (~+30-60s if any retries fire).
    est_min_s = max(20, len(picked_filenames) * 25)
    est_max_s = len(picked_filenames) * 90
    st.caption(
        f"Estimated {est_min_s}–{est_max_s}s for "
        f"{len(picked_filenames)} file(s) → {target_cloud.upper()} "
        f"(~25-90s per file: blueprint + generate + up to 3 "
        f"validate-feedback retries)."
    )


# --- Stage C: Results card (shown when last_result exists) -------------

last_result = st.session_state.get(_SS_LAST_RESULT)
if last_result and not translate_button:
    st.markdown("---")
    st.markdown("### Last translation result")
    _last_target = last_result.get("target_cloud", "?").upper()
    st.success(
        f"✅ Last batch ({_last_target}) completed in "
        f"{last_result.get('duration_s', 0):.1f}s",
        icon="✅",
    )
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Translated", last_result.get("translated", 0))
    m2.metric("Needs attention", last_result.get("needs_attention", 0))
    m3.metric("Failed", last_result.get("failed", 0))
    m4.metric("Skipped", last_result.get("skipped", 0))
    with st.expander("Full result (structured)", expanded=False):
        st.json(last_result)


# --- Generated translated files (per-target expander) ------------------

st.markdown("---")
# PUI-3a: only render expanders for targets in the allowlist.
# AWS-only deploys hide the Azure expander entirely.
_ALL_TARGET_EXPANDERS = (
    ("aws", _SS_TRANSLATED_AWS),
    ("azure", _SS_TRANSLATED_AZURE),
)
for target, ss_key in (
    e for e in _ALL_TARGET_EXPANDERS if e[0] in _UI_TARGETS
):
    badge = "🟧 AWS" if target == "aws" else "🟦 Azure"
    with st.expander(
        f"📄 Generated {badge} translations for this project",
        expanded=False,
    ):
        st.caption(
            f"Cumulative view: every `.tf` file persisted under "
            f"`translated/{target}/` for this project, across all "
            f"prior translation runs."
        )
        refresh_col, _spacer = st.columns([1, 4])
        with refresh_col:
            if st.button(
                "↻ Refresh list",
                key=f"tx_refresh_{target}",
            ):
                st.session_state.pop(ss_key, None)

        # Lazy fetch.
        if ss_key not in st.session_state:
            try:
                from common.storage import (  # noqa: E402
                    list_translated_files,
                )
                st.session_state[ss_key] = list_translated_files(
                    project_id, target,
                )
            except Exception as _e:  # noqa: BLE001
                st.warning(
                    f"Couldn't list {target.upper()} translations: "
                    f"`{type(_e).__name__}: {_e}`",
                    icon="⚠️",
                )
                st.session_state[ss_key] = []

        files_for_target = st.session_state.get(ss_key, [])
        if not files_for_target:
            st.caption(
                f"No {target.upper()} translations yet. Pick source "
                f"files above + target → Translate to populate this "
                f"section."
            )
        else:
            from common.storage import read_translated_file  # noqa: E402
            for tf_file in files_for_target:
                fname = tf_file["name"]
                size_kb = tf_file["size_bytes"] / 1024
                with st.expander(
                    f"✅  `{fname}`  ({size_kb:.1f} KB)",
                    expanded=False,
                ):
                    try:
                        content = read_translated_file(
                            project_id, target, fname,
                        )
                    except Exception as _e:  # noqa: BLE001
                        st.error(
                            f"Failed to read `{fname}`: "
                            f"`{type(_e).__name__}: {_e}`",
                        )
                        continue
                    st.code(content, language="hcl")
                    st.download_button(
                        label=f"📥 Download {fname}",
                        data=content,
                        file_name=fname,
                        mime="text/plain",
                        key=f"dl_{target}_{fname}",
                    )


if not translate_button:
    st.stop()


# --- Live translation path ---------------------------------------------

if _lock is not None:
    st.warning(
        "Translation already in progress; ignoring click.", icon="⚠️",
    )
    st.stop()

# Immediate visual feedback (mirrors Inventory's pattern):
#   1. Swap the Translate button to a disabled "Translating..." state.
#   2. Browser toast.
#   3. Green in-page banner.
translate_btn_slot.button(
    f"⚡ Translating ({len(picked_filenames)} file(s)) → "
    f"{target_cloud.upper()}…",
    type="secondary",
    disabled=True,
    key="tx_translate_btn_disabled_swap",
    use_container_width=True,
)
st.toast(
    f"⚡ Starting translation: {len(picked_filenames)} file(s) → "
    f"{target_cloud.upper()}...",
    icon="🚀",
)
st.success(
    f"🚀 Translation started for **{project_id}** → "
    f"{target_cloud.upper()} ({len(picked_filenames)} file(s) "
    f"selected). Loading engine modules...",
    icon="🚀",
)

# Acquire lock immediately.
st.session_state[_SS_RUN_LOCK] = {
    "start_ts": _time.time(),
    "project_id": project_id,
    "target_cloud": target_cloud,
    "selected_count": len(picked_filenames),
}

from app.middleware import workdir_context  # noqa: E402
from translator.run import run_translation_batch  # noqa: E402

started = time.monotonic()
try:
    with st.spinner(
        f"Translating {len(picked_filenames)} file(s) → "
        f"{target_cloud.upper()} … "
        f"(~25-90s per file; LLM blueprint + generate + retries)"
    ):
        with workdir_context(project_id) as workdir:
            # Resolve each picked filename to its absolute path in the
            # hydrated /tmp workdir. The translator engine reads from
            # disk; persist_workdir on workdir_context exit syncs the
            # generated translated/<target>/*.tf back to GCS.
            source_paths = [
                os.path.join(workdir, fname)
                for fname in picked_filenames
            ]
            result = run_translation_batch(
                target_cloud=target_cloud,
                source_paths=source_paths,
                tenant_id="default",
                project_id=project_id,
            )
except Exception as e:  # noqa: BLE001
    st.session_state.pop(_SS_RUN_LOCK, None)
    render_error(
        e, context=(
            f"translating {len(picked_filenames)} file(s) → "
            f"{target_cloud}"
        ),
    )
    st.stop()

# Clean exit: clear lock, cache result, refresh.
st.session_state.pop(_SS_RUN_LOCK, None)
duration = time.monotonic() - started
result_dict = result.as_fields()
result_dict["duration_s"] = round(duration, 2)
# PUI-3b: stamp the wall-clock at cache time so the auto-recover at
# the top of this page can detect "result is newer than the
# currently-held lock" -> stale lock from a websocket-dropped run
# -> safe to clear.
result_dict["_cached_at"] = _time.time()
st.session_state[_SS_LAST_RESULT] = result_dict

# Bust the per-target file-list cache so the expander refetches.
if target_cloud == "aws":
    st.session_state.pop(_SS_TRANSLATED_AWS, None)
else:
    st.session_state.pop(_SS_TRANSLATED_AZURE, None)

st.rerun()
