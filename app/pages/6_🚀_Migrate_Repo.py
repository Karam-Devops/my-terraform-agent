# app/pages/6_🚀_Migrate_Repo.py
"""Migrator engine page — Phase 7 Git-IaC Translator UI.

Three-stage workflow:

  Stage A — INPUT:
    Operator pastes a local path (or future: Git URL) of a customer
    repo, picks the target cloud (AWS today). The Platform validates
    the path exists and python-hcl2 is available, then enables the
    "Run Migration" button.

  Stage B — MIGRATE:
    One click runs the full Discover → Plan → Generate pipeline.
    Wall clock for the simple-gcp fixture: ~2-5 seconds. For the
    customer's 1,050-file Terragrunt repo: under 60s target.

  Stage C — RESULTS:
    Tabs render:
      Inventory   — every resource discovered, grouped by module
      Confidence  — per-resource HIGH/MEDIUM/LOW/MANUAL_REVIEW with reason
      Dep Graph   — directed edges between resources
      Migration Guide — rendered MIGRATION_GUIDE.md inline
      Output Files — list + download buttons for everything generated

Engine wiring:
  * migrator.run.run_migration(repo_path, target_cloud, ...)
    is the headless entry point.
  * Output files land in <repo_path>/migrator_output/ by default,
    or in a Streamlit-controlled tempdir if the operator's repo
    is read-only.

Theme: dark theme polish via apply_theme_polish() to match the rest
of the app.
"""

from __future__ import annotations

import os
import shutil
import tempfile
import time
from pathlib import Path
from typing import Optional

import streamlit as st

from app.ui.error_surface import render_error
from app.ui.sidebar import render_sidebar
from app.ui.theme import apply_theme_polish
from common.errors import PreflightError
from migrator import config as migrator_config
from migrator.ingest.hcl_parser import is_hcl_parser_available
from migrator.results import (
    CONFIDENCE_HIGH,
    CONFIDENCE_LOW,
    CONFIDENCE_MANUAL,
    CONFIDENCE_MEDIUM,
    MigrationResult,
)
from migrator.run import run_migration


def _band_with_emoji(band: str) -> str:
    return {
        CONFIDENCE_HIGH:    "🟢 HIGH",
        CONFIDENCE_MEDIUM:  "🟡 MEDIUM",
        CONFIDENCE_LOW:     "🔴 LOW",
        CONFIDENCE_MANUAL:  "⚠️ MANUAL_REVIEW",
    }.get(band, band)


# Page chrome
st.set_page_config(
    page_title="mtagent · Migrate Repo",
    page_icon="🚀",
    layout="wide",
)
apply_theme_polish()

# Sidebar (project picker is informational here — Migrator is repo-driven,
# not project-driven, so project_id is purely for log tagging).
project_id = render_sidebar()

st.title("🚀 Migrate Repo")
st.caption(
    "End-to-end GCP → AWS migration. Point the engine at a Terraform / "
    "Terragrunt repo, and the Platform discovers every resource, scores "
    "translation confidence, builds a dependency graph, and emits "
    "MIGRATION_GUIDE.md with helper scripts."
)

# Preflight: hcl2 must be installed.
if not is_hcl_parser_available():
    st.error(
        "🛑 **`python-hcl2` is not installed in this deployment.** "
        "Add `python-hcl2` to `requirements.txt` and rebuild the Cloud Run image. "
        "The Migrator engine cannot parse customer HCL until this dependency lands.",
        icon="🛑",
    )
    st.stop()


# ============================================================
# STAGE A — INPUT
# ============================================================

st.subheader("1. Source repository")

with st.form(key="migrator_form"):
    repo_path_input = st.text_input(
        "Local path to GCP repo",
        value=st.session_state.get(
            "migrator_repo_path",
            r"C:\Users\41708\gcp-iac-fixtures\simple-gcp",
        ),
        help=(
            "Absolute path to a checked-out customer repo. "
            "Demo input: `C:\\Users\\41708\\gcp-iac-fixtures\\simple-gcp` "
            "(vanilla TF) or `\\complex-gcp-terragrunt` (Terragrunt). "
            "Future: paste a GitHub URL and the Platform clones it."
        ),
    )

    target_cloud_choice = st.radio(
        "Target cloud",
        options=migrator_config.MIGRATOR_TARGETS_ALLOWED,
        horizontal=True,
        format_func=lambda t: t.upper(),
    )

    submitted = st.form_submit_button(
        "🚀 Run Migration",
        type="primary",
        use_container_width=False,
    )


# ============================================================
# STAGE B — MIGRATE
# ============================================================

if submitted:
    if not repo_path_input or not repo_path_input.strip():
        st.error("Please enter a repo path.", icon="❌")
        st.stop()

    repo_path = repo_path_input.strip()
    st.session_state["migrator_repo_path"] = repo_path

    if not os.path.isdir(repo_path):
        st.error(f"Repo path does not exist or is not a directory: `{repo_path}`", icon="❌")
        st.stop()

    # Output dir: prefer the repo's own migrator_output/, but fall back
    # to a tempdir if the repo path isn't writable (read-only volume,
    # etc.). Demo case: writes go into the fixture repo, which is fine.
    requested_output = os.path.join(repo_path, migrator_config.MIGRATOR_OUTPUT_DIRNAME)
    try:
        os.makedirs(requested_output, exist_ok=True)
        # Touch-test for write permission.
        _test_path = os.path.join(requested_output, ".writetest")
        with open(_test_path, "w") as _t:
            _t.write("ok")
        os.remove(_test_path)
        output_dir = requested_output
    except OSError:
        # Read-only repo. Use a Streamlit-managed tempdir keyed by
        # repo path so re-runs of the same repo overwrite cleanly.
        output_dir = tempfile.mkdtemp(prefix="migrator_out_")

    progress = st.progress(0, text="Starting migration…")
    status = st.empty()

    try:
        status.info("Discover → Plan → Generate", icon="⚙️")
        progress.progress(15, text="Walking repo + parsing HCL…")
        started = time.monotonic()

        result: MigrationResult = run_migration(
            repo_path,
            target_cloud=target_cloud_choice,
            output_dir=output_dir,
            project_id=project_id,
        )

        progress.progress(100, text=f"Done in {round(time.monotonic() - started, 2)}s")
        status.empty()

    except PreflightError as e:
        progress.empty()
        status.empty()
        render_error(
            title="Preflight failure",
            error=e,
            user_hint=getattr(e, "user_hint", None) or str(e),
        )
        st.stop()
    except Exception as e:  # noqa: BLE001 — surface anything to the operator
        progress.empty()
        status.empty()
        st.exception(e)
        st.stop()

    st.session_state["migrator_last_result"] = result


# ============================================================
# STAGE C — RESULTS
# ============================================================

result: Optional[MigrationResult] = st.session_state.get("migrator_last_result")

if result is None:
    st.info("Configure a repo and click **Run Migration** to get started.", icon="👆")
    st.stop()


st.markdown("---")
st.subheader("2. Results")

# Hero metric strip
hero_a, hero_b, hero_c, hero_d, hero_e = st.columns(5)
with hero_a:
    st.metric("Resources discovered", result.resource_count)
with hero_b:
    st.metric("Files scanned", result.files_scanned)
with hero_c:
    st.metric("Source IaC", result.source_iac)
with hero_d:
    st.metric("Target cloud", result.target_cloud.upper())
with hero_e:
    st.metric("Wall clock (s)", result.duration_s)

# Confidence summary band
summary = result.confidence_summary
st.markdown("**Confidence breakdown**")
cb_a, cb_b, cb_c, cb_d = st.columns(4)
with cb_a:
    st.metric("🟢 HIGH",   summary.get(CONFIDENCE_HIGH, 0))
with cb_b:
    st.metric("🟡 MEDIUM", summary.get(CONFIDENCE_MEDIUM, 0))
with cb_c:
    st.metric("🔴 LOW",    summary.get(CONFIDENCE_LOW, 0))
with cb_d:
    st.metric("⚠️ MANUAL", summary.get(CONFIDENCE_MANUAL, 0))

if result.errors:
    with st.expander(f"⚠️ {len(result.errors)} ingest error(s) — click to see"):
        for err in result.errors:
            st.code(err, language="text")

# Tabs for the rest
tab_inv, tab_conf, tab_deps, tab_guide, tab_files = st.tabs([
    "📋 Inventory",
    "🎯 Confidence",
    "🔗 Dep Graph",
    "📖 Migration Guide",
    "💾 Output Files",
])

# ---------------- Inventory ----------------
with tab_inv:
    if not result.resources:
        st.warning("No resources discovered — is the repo path correct?")
    else:
        rows = [
            {
                "Address": r.address,
                "tf_type": r.tf_type,
                "Module": r.module_path,
                "File": os.path.relpath(r.file_path, result.repo_path)
                        if result.repo_path and r.file_path.startswith(result.repo_path)
                        else r.file_path,
            }
            for r in result.resources
        ]
        st.dataframe(
            rows,
            hide_index=True,
            use_container_width=True,
        )

# ---------------- Confidence ----------------
with tab_conf:
    if not result.confidence:
        st.warning("No confidence findings — check Inventory tab first.")
    else:
        # Build a sortable table; sort by ascending score so the
        # MANUAL_REVIEW + LOW items surface at the top.
        rows = [
            {
                "Resource": c.resource_address,
                "AWS equivalent": c.aws_equivalent or "(none)",
                "Band": _band_with_emoji(c.band),
                "Score": c.score_pct,
                "Reason": c.reason,
                "Notes": " · ".join(c.notes) if c.notes else "",
            }
            for c in sorted(result.confidence, key=lambda x: (x.score_pct, x.resource_address))
        ]
        st.dataframe(rows, hide_index=True, use_container_width=True)

# ---------------- Dep Graph ----------------
with tab_deps:
    if not result.dep_edges:
        st.info(
            "No inter-resource dependencies detected. "
            "(Single-module fixtures often don't have cross-references; "
            "complex multi-module repos will populate this tab.)"
        )
    else:
        rows = [
            {
                "Source (depends on…)": e.source,
                "→ Target": e.target,
                "via attr": e.via,
            }
            for e in result.dep_edges
        ]
        st.dataframe(rows, hide_index=True, use_container_width=True)

# ---------------- Migration Guide ----------------
with tab_guide:
    if not result.migration_guide_path or not os.path.isfile(result.migration_guide_path):
        st.warning("MIGRATION_GUIDE.md was not generated.")
    else:
        guide_md = Path(result.migration_guide_path).read_text(encoding="utf-8")
        st.markdown(guide_md)

        st.download_button(
            label="⬇ Download MIGRATION_GUIDE.md",
            data=guide_md,
            file_name="MIGRATION_GUIDE.md",
            mime="text/markdown",
            use_container_width=False,
        )

# ---------------- Output Files ----------------
with tab_files:
    if not result.output_dir or not os.path.isdir(result.output_dir):
        st.warning("Output directory was not created.")
    else:
        st.caption(f"Output directory: `{result.output_dir}`")

        # Walk the output dir + render every file with a download button.
        files_found = []
        for root, _dirs, fnames in os.walk(result.output_dir):
            for fname in sorted(fnames):
                full = os.path.join(root, fname)
                rel = os.path.relpath(full, result.output_dir).replace(os.sep, "/")
                files_found.append((rel, full))

        if not files_found:
            st.warning("No output files generated.")
        else:
            # Bundle as a zip for one-click download.
            with tempfile.NamedTemporaryFile(
                suffix=".zip", delete=False, prefix="migrator_bundle_"
            ) as zip_tmp:
                bundle_path = zip_tmp.name
            shutil.make_archive(
                bundle_path[:-4],  # shutil appends .zip
                "zip",
                result.output_dir,
            )
            with open(bundle_path, "rb") as zf:
                st.download_button(
                    label=f"⬇ Download all ({len(files_found)} files) as ZIP",
                    data=zf.read(),
                    file_name="migrator_output.zip",
                    mime="application/zip",
                    use_container_width=False,
                )
            try:
                os.remove(bundle_path)
            except OSError:
                pass

            st.markdown("**Files generated:**")
            for rel, full in files_found:
                with st.expander(rel):
                    try:
                        size = os.path.getsize(full)
                    except OSError:
                        size = 0
                    st.caption(f"{size} bytes")
                    if rel.endswith(".md") or rel.endswith(".sh") or rel.endswith(".tf") \
                       or rel.endswith(".hcl") or rel.endswith(".json"):
                        try:
                            content = Path(full).read_text(encoding="utf-8")
                            language = (
                                "markdown" if rel.endswith(".md")
                                else "bash" if rel.endswith(".sh")
                                else "json" if rel.endswith(".json")
                                else "hcl"
                            )
                            st.code(content, language=language)
                        except (OSError, UnicodeDecodeError):
                            st.warning("Could not read file content (non-text or unreadable).")
                    else:
                        st.caption("(binary or unsupported preview format)")


# Reset button
st.markdown("---")
with st.expander("⚙️ Reset"):
    if st.button("Clear last result", type="secondary"):
        st.session_state.pop("migrator_last_result", None)
        st.rerun()
