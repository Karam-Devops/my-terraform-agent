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
            r"C:\Users\41708\gcp-iac-fixtures\simple-gcp\environments\dev",
        ),
        help=(
            "Absolute path to a checked-out customer repo (or any subdirectory). "
            "Demo default points at the `dev` environment subset (~77 stacks). "
            "Pass the full repo root for an end-to-end run. "
            "Future: paste a Git URL and the Platform clones it."
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
tab_inv, tab_conf, tab_deps, tab_guide, tab_aws, tab_validate, tab_files = st.tabs([
    "📋 Inventory",
    "🎯 Confidence",
    "🔗 Dep Graph",
    "📖 Migration Guide",
    "🚀 AWS Skeleton",
    "🛡️ Validation",
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
        # Sort by band priority: HIGH → MEDIUM → LOW → MANUAL_REVIEW.
        # Within each band, descending score then resource address.
        _BAND_PRIORITY = {
            CONFIDENCE_HIGH:    0,
            CONFIDENCE_MEDIUM:  1,
            CONFIDENCE_LOW:     2,
            CONFIDENCE_MANUAL:  3,
        }
        # Detect which tf_types have translators registered (for the
        # Translator status column). Imports the dispatcher's TRANSLATORS
        # map so the UI doesn't go stale when new translators are added.
        from migrator.translate import TRANSLATORS as _TRANSLATORS
        _registered = set(_TRANSLATORS.keys())

        def _translator_status(c) -> str:
            if c.band == CONFIDENCE_MANUAL:
                return "🚫 N/A (no AWS analog)"
            if c.tf_type in _registered:
                return "✅ Translated"
            return "⏳ Scaffold (translator pending)"

        rows = [
            {
                "Resource": c.resource_address,
                "AWS equivalent": c.aws_equivalent or "(none)",
                "Band": _band_with_emoji(c.band),
                "Score": c.score_pct,
                "Translator": _translator_status(c),
                "Reason": c.reason,
                "Notes": " · ".join(c.notes) if c.notes else "",
            }
            for c in sorted(
                result.confidence,
                key=lambda x: (
                    _BAND_PRIORITY.get(x.band, 99),
                    -x.score_pct,
                    x.resource_address,
                ),
            )
        ]
        st.dataframe(rows, hide_index=True, use_container_width=True)
        st.caption(
            "**Translator** column shows whether a per-resource translator is registered. "
            "✅ Translated = populated `inputs = {...}` + AWS module body emitted. "
            "⏳ Scaffold = mapping known but no translator code yet (commented-out inputs block; operator fills in or we register a translator). "
            "🚫 N/A = no AWS equivalent."
        )

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
    # Executive summary first (operator-facing one-pager), then full guide.
    exec_summary_path = (
        os.path.join(result.output_dir, "EXECUTIVE_SUMMARY.md")
        if result.output_dir else None
    )
    if exec_summary_path and os.path.isfile(exec_summary_path):
        st.markdown("### 📊 Executive Summary")
        st.caption(
            "One-page customer-facing summary — share with CTO/CISO. "
            "The full deep-dive is below."
        )
        exec_md = Path(exec_summary_path).read_text(encoding="utf-8")
        col_dl, _spacer = st.columns([2, 6])
        with col_dl:
            st.download_button(
                label="⬇ EXECUTIVE_SUMMARY.md",
                data=exec_md,
                file_name="EXECUTIVE_SUMMARY.md",
                mime="text/markdown",
                use_container_width=True,
                key="dl_exec_summary",
            )
        with st.container(height=400, border=True):
            st.markdown(exec_md)
        st.markdown("---")

    if not result.migration_guide_path or not os.path.isfile(result.migration_guide_path):
        st.warning("MIGRATION_GUIDE.md was not generated.")
    else:
        guide_md = Path(result.migration_guide_path).read_text(encoding="utf-8")

        st.markdown("### 📖 Full Migration Guide")
        st.caption(
            "Full deploy-order sequence, per-resource confidence findings, "
            "rollback procedure. Shared with engineering."
        )

        st.download_button(
            label="⬇ Download MIGRATION_GUIDE.md",
            data=guide_md,
            file_name="MIGRATION_GUIDE.md",
            mime="text/markdown",
            use_container_width=False,
            key="dl_full_guide",
        )

        # Scrollable container — markdown for 941-stack repos is long;
        # giving it a fixed height with internal scroll keeps the rest
        # of the page navigable.
        with st.container(height=700, border=True):
            st.markdown(guide_md)

# ---------------- AWS Skeleton ----------------
with tab_aws:
    if not result.skeleton_paths:
        st.warning("AWS Terragrunt skeleton was not generated.")
    else:
        target_dir = os.path.join(result.output_dir or "", "target")

        st.markdown(
            f"**Generated AWS Terragrunt skeleton at:** `{target_dir}`"
        )
        st.caption(
            f"{len(result.skeleton_paths)} files emitted, mirroring the source "
            "`live/<env>/<region>/<stack>/` structure. Each `terragrunt.hcl` "
            "includes the source GCP context (module path, inputs as comments) "
            "plus a placeholder `terraform { source = ... }` block pointing at "
            "where your AWS module library should live."
        )
        st.markdown("---")

        # Show the AWS root config first — that's the most important file
        # for the operator to inspect.
        root_path = os.path.join(target_dir, "terragrunt.hcl")
        if os.path.isfile(root_path):
            with st.expander("🏠 Synthesized AWS root `terragrunt.hcl`", expanded=False):
                st.code(Path(root_path).read_text(encoding="utf-8"), language="hcl")

        # Then show the directory tree with filters.
        if os.path.isdir(target_dir):
            st.markdown("### 📁 Generated stack files")

            # Walk the tree and collect files first.
            tree_files = []
            for root, _dirs, fnames in os.walk(target_dir):
                for fname in sorted(fnames):
                    full = os.path.join(root, fname)
                    rel = os.path.relpath(full, target_dir).replace(os.sep, "/")
                    tree_files.append((rel, full))
            tree_files.sort()

            # Derive filter facets:
            #   - Top-level dirs (typically "modules", "_common",
            #     "environments", "common", or specific project dirs)
            #   - Detected service types from translator service_name
            #     dirs under modules/, plus inferred from path keywords
            top_dirs = sorted({rel.split("/")[0] for rel, _ in tree_files
                              if "/" in rel})
            # File extensions present
            extensions = sorted({"." + rel.rsplit(".", 1)[1] if "." in rel.rsplit("/", 1)[-1] else "(no ext)"
                                 for rel, _ in tree_files})

            # Render filter row.
            f_col1, f_col2, f_col3 = st.columns([2, 2, 3])
            with f_col1:
                top_filter = st.multiselect(
                    "Top-level dir",
                    options=top_dirs,
                    default=[],
                    help="Filter by top-level directory under `target/`. "
                         "Empty = all directories.",
                    key="aws_skel_top_filter",
                )
            with f_col2:
                ext_filter = st.multiselect(
                    "File type",
                    options=extensions,
                    default=[],
                    help="Filter by extension. Empty = all extensions.",
                    key="aws_skel_ext_filter",
                )
            with f_col3:
                search_term = st.text_input(
                    "Search path (substring match)",
                    value="",
                    help="Show only files whose path contains this substring (case-insensitive).",
                    key="aws_skel_search",
                )

            # Apply filters
            filtered = []
            search_lower = search_term.strip().lower()
            for rel, full in tree_files:
                top = rel.split("/")[0] if "/" in rel else "(root)"
                if top_filter and top not in top_filter:
                    continue
                ext = "." + rel.rsplit(".", 1)[1] if "." in rel.rsplit("/", 1)[-1] else "(no ext)"
                if ext_filter and ext not in ext_filter:
                    continue
                if search_lower and search_lower not in rel.lower():
                    continue
                filtered.append((rel, full))

            st.caption(
                f"Showing **{len(filtered):,}** of {len(tree_files):,} files. "
                f"Adjust filters above to narrow."
                + (f" Use the **💾 Output Files** tab to download the full ZIP." if len(filtered) > 100 else "")
            )

            if not filtered:
                st.info("No files match the current filters.", icon="🔎")
            else:
                # Group filtered by top-level dir (or 2 levels deep if
                # only 1 top-level dir is selected — keeps the expander
                # count manageable when filtering down to a single env).
                from collections import OrderedDict
                grouped: "OrderedDict[str, list]" = OrderedDict()

                # If user has filtered to a single top dir, group by
                # second-level (e.g. environments/dev/* shows by project).
                use_two_level = len(top_filter) == 1
                for rel, full in filtered:
                    parts = rel.split("/")
                    if use_two_level and len(parts) >= 2:
                        group_key = "/".join(parts[:2])
                    else:
                        group_key = parts[0] if "/" in rel else "(root)"
                    grouped.setdefault(group_key, []).append((rel, full))

                # Pagination — cap how many EXPANDERS we render at once.
                # Each expander contains a group of files. With 1,050
                # source files, pagination keeps DOM-element count sane.
                MAX_GROUPS_PER_PAGE = 20
                total_groups = len(grouped)
                if total_groups > MAX_GROUPS_PER_PAGE:
                    page = st.number_input(
                        f"Page (1 to {(total_groups + MAX_GROUPS_PER_PAGE - 1) // MAX_GROUPS_PER_PAGE})",
                        min_value=1,
                        max_value=(total_groups + MAX_GROUPS_PER_PAGE - 1) // MAX_GROUPS_PER_PAGE,
                        value=1,
                        step=1,
                        key="aws_skel_page",
                    )
                    start = (page - 1) * MAX_GROUPS_PER_PAGE
                    end = start + MAX_GROUPS_PER_PAGE
                    visible_groups = list(grouped.items())[start:end]
                    st.caption(f"Showing groups {start + 1}-{min(end, total_groups)} of {total_groups}.")
                else:
                    visible_groups = list(grouped.items())

                # Per-group cap on file content rendering (large groups
                # show summary + first-N).
                FILE_CAP_PER_GROUP = 25
                for top, members in visible_groups:
                    with st.expander(f"📂 `{top}/`  ({len(members)} files)"):
                        for rel, full in members[:FILE_CAP_PER_GROUP]:
                            st.markdown(f"**`{rel}`**")
                            try:
                                content = Path(full).read_text(encoding="utf-8")
                                st.code(content, language="hcl" if rel.endswith(".hcl")
                                        else "markdown" if rel.endswith(".md")
                                        else "hcl")
                            except (OSError, UnicodeDecodeError):
                                st.caption("(could not read)")
                        if len(members) > FILE_CAP_PER_GROUP:
                            st.info(
                                f"... and {len(members) - FILE_CAP_PER_GROUP} more files in `{top}/`. "
                                f"Refine filters above to narrow further, or use the **💾 Output Files** tab to download the full ZIP."
                            )


# ---------------- Validation (Tiers 0–3) ----------------
with tab_validate:
    val = result.validation or {}
    if not val:
        st.warning("Validation was not run.")
    else:
        overall = val.get("overall_passed")
        if overall:
            st.success(
                f"✅ Validation passed — every available tier reports clean. "
                f"Total wall clock: {val.get('total_duration_s', 0)}s.",
                icon="✅",
            )
        else:
            st.error(
                f"⚠️ Validation has failures or skipped tiers — see per-tier breakdown below. "
                f"Total wall clock: {val.get('total_duration_s', 0)}s.",
                icon="⚠️",
            )

        st.markdown("---")
        st.markdown("### Tier-by-tier results")
        st.caption(
            "Tiers 0–3 require no cloud credentials and run automatically. "
            "Tiers 4–6 (`terragrunt run-all validate` / `plan` / `apply`) are deferred — "
            "they need AWS sandbox credentials. See the strategy memory."
        )

        tiers = val.get("tiers") or []
        for t in tiers:
            tier_num = t.get("tier", "?")
            name = t.get("name", "")
            status = t.get("status", "unknown")
            files_checked = t.get("files_checked", 0)
            failure_count = t.get("failure_count", 0)
            skip_reason = t.get("skip_reason", "")

            badge = {
                "passed":  "🟢 PASSED",
                "failed":  "🔴 FAILED",
                "skipped": "⚪ SKIPPED",
            }.get(status, "❓")

            label = f"Tier {tier_num} — {name}    {badge}"
            with st.expander(label, expanded=(status == "failed")):
                col1, col2, col3 = st.columns(3)
                with col1:
                    st.metric("Files checked", files_checked)
                with col2:
                    st.metric("Failures", failure_count)
                with col3:
                    st.metric("Status", status)
                if skip_reason:
                    st.info(f"Skipped: {skip_reason}", icon="ℹ️")
                # Note: failure details not in summary dict (only count).
                # For detail surfacing in v2, store full failures list.

        st.markdown("---")
        st.markdown(
            "**What's not yet automated** (deferred to v2 per `phase7_migrator_strategy` memory):"
        )
        st.markdown(
            "- **Tier 4** — `terragrunt run-all validate` (real AWS provider schema check). "
            "Needs cloud credentials.\n"
            "- **Tier 5** — `terragrunt run-all plan -input=false`. Needs cloud credentials + state backend.\n"
            "- **Tier 6** — apply-and-destroy on a sandbox AWS account. Needs sandbox + budget guard.\n"
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
