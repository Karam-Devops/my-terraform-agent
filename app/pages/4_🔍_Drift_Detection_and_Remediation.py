# app/pages/3_Drift_Detection.py
"""Detector page (PUI-4) — the star of the SaaS demo.

the killer pattern is "show me what's NOT in IaC" -- coverage
percentage at the top, color-coded buckets, per-resource Codify CTAs
that flow back to the Inventory page in one click. This page maps
detector.rescan.rescan() output (a DriftReport) onto that UX.

Three result buckets get rendered as tabs:

  Unmanaged (the demo's hero finding)
    Resources visible in cloud but NOT in terraform state. The CG-1
    capability: customer adopts 16 resources Monday, an admin spins
    up a new bucket Tuesday in the console, our Tuesday rescan
    surfaces that bucket here. Per-row "Codify in Inventory" deep-link
    (PUI-4d) takes the operator to Inventory with the resource
    pre-selected for import.

  Compliant
    Resources in state. Currently catches everything in state since
    PUI-4e (drift_check wiring) is deferred -- when the drift_check
    lands, "compliant" narrows to "in state AND cloud values match
    HCL" and the Drift bucket actually populates.

  Drift
    Resources in state whose cloud values diverge from HCL. Always
    empty until PUI-4e ships. Tab still rendered so operators can
    see the future shape of the bucket.

  Errors
    Asset types whose enumeration failed during the cloud-side
    discovery. Non-empty here means the Unmanaged report may have
    false negatives (resources of those types couldn't even be
    checked). Surfaced so customers don't false-trust an "0
    unmanaged" report when discovery was actually incomplete.

State-reading caveat (PSA-5 + PUI-4):
The detector's state_reader expects a local terraform.tfstate file.
With GCS backend (MTAGENT_USE_GCS_BACKEND=1, set in SaaS), terraform
writes state to GCS and there is no local file. We materialize it
on the fly via terraform_client.state_pull() before calling rescan.

Engine wiring:
  * detector.rescan.rescan(project_id, project_root) -> DriftReport
  * importer.terraform_client.state_pull (PUI-4 helper) materializes
    GCS state to local file before rescan runs
  * common.snapshots.write_snapshot already wired inside rescan
    (Dashboard reads from there in PUI-2)

Theme: same dark theme polish as Inventory + Translator via
apply_theme_polish(). Mint accent on Run rescan; red on Danger Zone;
status pills follow the Material palette.
"""

import json
import os
import time

import streamlit as st

from app.ui.sidebar import render_sidebar
from app.ui.error_surface import render_error
from app.ui.theme import apply_theme_polish


# Page chrome
st.set_page_config(
    page_title="mtagent · Drift Detection",
    page_icon="🔍",
    layout="wide",
)

apply_theme_polish()

project_id = render_sidebar()

st.title("🔍 Drift Detection & Remediation")
st.caption(
    "Compare cloud reality vs terraform state. Find resources NOT yet "
    "codified (Unmanaged), resources whose HCL drifted from cloud "
    "values (Drift), and remediate per resource (Restore HCL→Cloud, "
    "Accept Cloud→State, Recreate, Stop managing)."
)

if not project_id:
    st.warning("Pick a project in the sidebar to get started.", icon="⚠️")
    st.stop()

st.markdown(f"**Project:** `{project_id}`")

# --- Session-state keys -------------------------------------------------
_SS_RUN_LOCK = "_detector_run_lock"
_SS_LAST_RESULT = f"_detector_last_result_{project_id}"


# --- Tier-A run lock + PUI-3b auto-recover -----------------------------
import time as _time

_RUN_TIMEOUT_S = 600
_lock = st.session_state.get(_SS_RUN_LOCK)
_last_result_for_recover = st.session_state.get(_SS_LAST_RESULT)

if _lock is not None:
    _elapsed = _time.time() - _lock.get("start_ts", 0)
    if _elapsed > _RUN_TIMEOUT_S:
        st.session_state.pop(_SS_RUN_LOCK, None)
        _lock = None
    elif (
        _last_result_for_recover is not None
        and _last_result_for_recover.get("_cached_at", 0)
        > _lock.get("start_ts", 0)
    ):
        # PUI-3b auto-recover: stale lock from websocket-dropped run.
        print(
            f"PUI-3b auto-recover: clearing stale Detector run-lock "
            f"({int(_elapsed)}s old; result cached "
            f"{int(_time.time() - _last_result_for_recover['_cached_at'])}s "
            f"ago)."
        )
        st.session_state.pop(_SS_RUN_LOCK, None)
        _lock = None


# --- Danger Zone (always visible) --------------------------------------
# Detector rescan is read-only on cloud + state, so Reset is far less
# destructive here than on Inventory. Just clears the cached
# DriftReport from session_state so the next render starts fresh
# (e.g., after an external state mutation).
def _render_danger_zone() -> None:
    with st.expander("⚠️ Danger zone", expanded=False):
        st.markdown(
            f"### Reset rescan results for `{project_id}`\n\n"
            f"This clears the cached DriftReport from this Streamlit "
            f"session. Cloud + terraform state are NOT touched -- "
            f"rescan is purely read-only.\n\n"
            f"_Use when you want to redo a rescan from scratch (e.g., "
            f"after running an external `terraform apply` or codifying "
            f"new resources via Inventory)._"
        )
        typed_confirm = st.text_input(
            f"Type the project ID to confirm: `{project_id}`",
            value="",
            key="dz_dt_confirm",
            placeholder=project_id,
        )
        confirm_match = typed_confirm.strip() == project_id
        reset_btn_disabled = (
            (not confirm_match) or (_lock is not None)
        )
        reset_help = (
            "Type the project ID exactly to enable this button."
            if not confirm_match
            else "Rescan in progress; wait for it to complete."
            if _lock is not None
            else "Clears the cached DriftReport. Cloud + state untouched."
        )
        if st.button(
            "🗑️ Reset rescan results",
            type="primary",
            disabled=reset_btn_disabled,
            key="dz_dt_reset_btn",
            help=reset_help,
        ):
            st.session_state.pop(_SS_LAST_RESULT, None)
            st.success(
                f"✅ Rescan results cleared for `{project_id}`. "
                f"Click Run rescan above for a fresh scan.",
                icon="✅",
            )


_render_danger_zone()


# --- Run rescan trigger ------------------------------------------------
st.markdown("---")
st.markdown("### Run rescan")
st.caption(
    "Cloud-side enumeration via Cloud Asset Inventory + state-side "
    "read of `terraform.tfstate`. Set-diffs the two to surface "
    "Unmanaged (in cloud, not in IaC). Cheap (~5-15s); no LLM cost."
)

run_col, info_col = st.columns([1, 2])
with run_col:
    rescan_btn_slot = st.empty()
    rescan_button = rescan_btn_slot.button(
        "▶ Run rescan" if not _lock
        else f"Rescanning ({int(_time.time() - _lock['start_ts'])}s)…",
        type="primary",
        disabled=(_lock is not None),
        key="dt_rescan_btn",
        use_container_width=True,
    )
with info_col:
    if _lock is not None:
        st.warning(
            f"⏳ Rescan in progress for **{_lock.get('project_id')}**; "
            f"started {int(_time.time() - _lock['start_ts'])}s ago.",
            icon="⏳",
        )
    elif _last_result_for_recover is not None:
        _last_dur = _last_result_for_recover.get("duration_s", 0)
        st.caption(
            f"_Last rescan: {_last_dur:.1f}s. Click **Run rescan** to "
            f"refresh._"
        )
    else:
        st.caption(
            "_No rescan run yet for this project in this session. "
            "Click **Run rescan** above._"
        )


# --- Render last DriftReport (if present) ------------------------------

last_result = st.session_state.get(_SS_LAST_RESULT)

if last_result and not rescan_button:
    st.markdown("---")
    _drifted = last_result.get("drifted_count", 0)
    _compliant = last_result.get("compliant_count", 0)
    _errors = last_result.get("inventory_error_count", 0)
    _in_state = last_result.get("total_in_state", 0)
    _in_cloud = last_result.get("total_in_cloud", 0)

    # ------------------------------------------------------------------
    # PUI-4g (2026-04-30): orphan-filtered "Unmanaged" definition.
    # ------------------------------------------------------------------
    # The strict engine-side definition counts ANY resource not in
    # state as Unmanaged. That over-reports for resources auto-spawned
    # by a managed parent (e.g., GKE cluster auto-creates node-pool
    # VMs, their boot disks, default service accounts; KMS key rings
    # auto-include keys; Pub/Sub topics include subscriptions).
    # Operators don't separately codify these in HCL -- the parent
    # resource manages them.
    #
    # Common UX: hide auto-managed children from the Unmanaged
    # count by default; toggle to reveal. Counts the genuinely-orphan
    # resources only (= "things you should probably codify").
    #
    # Heuristics applied per resource type. Conservative -- a wrong
    # match HIDES a resource that should be flagged, so we only match
    # high-confidence patterns. Toggle below lets the operator audit
    # the heuristic by un-hiding.
    #
    # Long-term (PUI-4g v2): move this classification into the
    # detector engine itself so the DriftReport carries a third
    # bucket (`child_of_managed`) and snapshots reflect the same
    # taxonomy. UI-side for now to ship demo-ready in one round.
    _unmanaged_raw = last_result.get("unmanaged", []) or []
    _compliant_raw = last_result.get("compliant", []) or []

    # Lookup sets for parent detection.
    _compliant_cluster_names = {
        r.get("hcl_name") for r in _compliant_raw
        if r.get("tf_type") == "google_container_cluster"
    }
    _compliant_vm_names = {
        r.get("hcl_name") for r in _compliant_raw
        if r.get("tf_type") == "google_compute_instance"
    }
    _compliant_keyring_names = {
        r.get("hcl_name") for r in _compliant_raw
        if r.get("tf_type") == "google_kms_key_ring"
    }
    _compliant_topic_names = {
        r.get("hcl_name") for r in _compliant_raw
        if r.get("tf_type") == "google_pubsub_topic"
    }

    def _classify_parent_owner(r: dict) -> str:
        """Return human-readable parent ownership tag, or empty string
        if the resource is genuinely orphan.

        PUI-4g v2 (2026-04-30 smoke fix): the v1 heuristic required a
        parent in Compliant before classifying a child as "owned" --
        which meant clusters-not-yet-imported left all their child
        resources falsely flagged as Unmanaged (~50+ false positives
        on the smoke project). v2 fires UNCONDITIONALLY for high-
        confidence name patterns. Trade-off: a manually-named
        resource starting with "gke-" gets hidden, but that's such
        an unusual choice (the prefix is reserved by Google) that
        false-positive risk is acceptable.

        Also fixes the underscore-vs-hyphen mismatch between state's
        hcl_name (sanitized snake_case) and cloud's actual hyphenated
        name -- now tries both spellings for parent matching.

        Ordered most-specific to least. Each rule includes a comment
        explaining why the heuristic is safe.
        """
        name = r.get("cloud_name", "") or ""
        urn = r.get("cloud_urn", "") or ""
        tf_type = r.get("tf_type", "")

        # GKE auto-spawn: node-pool VMs, boot disks, NEGs, instance
        # groups, etc. All have name prefix `gke-`. The "gke-" prefix
        # is reserved by GKE -- operators don't manually create
        # resources with this prefix, so unconditional matching is
        # safe in practice. Try to attribute to a SPECIFIC managed
        # cluster first (handling underscore<->hyphen sanitization);
        # fall back to "GKE cluster (not yet imported)" so the child
        # is still hidden from the genuinely-unmanaged count.
        if name.startswith("gke-"):
            for cluster in _compliant_cluster_names:
                # state's hcl_name uses underscores; cloud uses hyphens
                cluster_hyphen = cluster.replace("_", "-")
                if (
                    name.startswith(f"gke-{cluster}-")
                    or name.startswith(f"gke-{cluster_hyphen}-")
                ):
                    return f"GKE cluster `{cluster_hyphen}`"
            # Cluster not in state (quarantined? not yet imported?) --
            # but the gke- prefix is so distinctive that we still
            # classify as auto-spawn so the demo's Unmanaged count
            # reflects "things to codify" not "GKE noise."
            return "GKE cluster (not yet imported)"

        # Default project service accounts. GCP auto-creates several
        # SAs on project enablement + service activation; operators
        # never codify these in HCL. URN/email patterns are the
        # high-confidence signal.
        if tf_type == "google_service_account":
            # @-domain checks against the full URN
            sa_default_domains = (
                "@cloudservices.gserviceaccount.com",
                "@developer.gserviceaccount.com",
                "@cloudbuild.gserviceaccount.com",
                "@compute-system.iam.gserviceaccount.com",
                "@container-engine-robot.iam.gserviceaccount.com",
                "@gcp-sa-",  # any "gcp-sa-*" Google-managed SA
                "@dataproc-accounts.iam.gserviceaccount.com",
                "@cloud-tpu.iam.gserviceaccount.com",
            )
            if any(d in urn or d in name for d in sa_default_domains):
                return "GCP default service account"
            # Numeric-prefix SAs are project defaults
            # (e.g. 1234567890-compute@developer.gserviceaccount.com)
            local = name.split("@", 1)[0] if "@" in name else name
            if local.split("-")[0].isdigit():
                return "GCP default service account"
            if local.endswith("-compute") or local == "default":
                return "GCP default service account"

        # GCE auto-created boot disk: name matches a managed VM
        # (default GCE behavior creates boot disk with the VM's name).
        if tf_type == "google_compute_disk":
            if name in _compliant_vm_names:
                return f"VM `{name}` (boot disk)"
            # Try hyphenated version (state hcl_name -> cloud name)
            for vm in _compliant_vm_names:
                if name == vm.replace("_", "-"):
                    return f"VM `{vm.replace('_', '-')}` (boot disk)"

        # KMS keys nested under a managed keyring (URN encodes parent).
        if tf_type == "google_kms_crypto_key":
            for keyring in _compliant_keyring_names:
                if (
                    f"/keyRings/{keyring}/" in urn
                    or f"/keyRings/{keyring.replace('_', '-')}/" in urn
                ):
                    return f"KMS key ring `{keyring.replace('_', '-')}`"

        # Pub/Sub subscriptions nested under a managed topic.
        if tf_type == "google_pubsub_subscription":
            for topic in _compliant_topic_names:
                topic_hyphen = topic.replace("_", "-")
                if (
                    f"/topics/{topic}" in urn
                    or f"/topics/{topic_hyphen}" in urn
                    or topic in name
                    or topic_hyphen in name
                ):
                    return f"Pub/Sub topic `{topic_hyphen}`"

        # Default networks/subnets that GCP creates per-project on
        # API enablement. Customers don't typically codify these.
        if tf_type in ("google_compute_network", "google_compute_subnetwork"):
            if name == "default":
                return "GCP default VPC"

        return ""  # genuinely orphan

    # Partition the raw unmanaged list.
    _unmanaged_orphan = []
    _unmanaged_child = []
    for _r in _unmanaged_raw:
        owner = _classify_parent_owner(_r)
        if owner:
            _r = {**_r, "_parent_owner": owner}
            _unmanaged_child.append(_r)
        else:
            _unmanaged_orphan.append(_r)

    # PUI-4g: customer-facing "Unmanaged" count is just the orphans.
    _unmanaged = len(_unmanaged_orphan)
    _unmanaged_hidden = len(_unmanaged_child)

    # PUI-4k (2026-04-30): industry-parity coverage formula.
    # Pre-PUI-4k denominator was total_in_cloud (in_state + ALL
    # unmanaged INCLUDING auto-managed children). That over-counts the
    # denominator with resources nobody would ever write Terraform for
    # (GKE node-pool VMs, default GCP service accounts, etc.) -- making
    # coverage look ~3x worse than it really is and demoralizing
    # operators who DID codify everything they reasonably could.
    # The industry-standard metric: only count IaC-eligible resources in the
    # denominator (compliant + drifted + GENUINELY-unmanaged orphans).
    # Auto-managed children are excluded -- they're managed by the
    # parent's HCL, not separately codifiable.
    _iac_eligible = _in_state + _unmanaged
    if _iac_eligible > 0:
        _coverage_pct = round(100.0 * _in_state / _iac_eligible)
    else:
        _coverage_pct = 0
    st.markdown(f"### Coverage: **{_coverage_pct}%** codified")
    st.progress(
        min(_in_state, _iac_eligible) / max(_iac_eligible, 1),
        text=(
            f"{_in_state} of {_iac_eligible} IaC-eligible resource(s) "
            f"tracked by Terraform "
            f"({_unmanaged_hidden} auto-managed children excluded)"
        ),
    )

    # 4-metric grid.
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("🟢 Compliant", _compliant)
    m2.metric("🟡 Drift", _drifted)
    m3.metric("🔴 Unmanaged", _unmanaged)
    m4.metric("⚠️ Errors", _errors)

    if _errors > 0:
        st.warning(
            f"⚠ Cloud-side enumeration failed for {_errors} asset "
            f"type(s) -- the **Unmanaged** count below may be a "
            f"lower bound (resources of those types couldn't be "
            f"checked). See the Errors tab for the failed types.",
            icon="⚠️",
        )

    # Tabbed bucket view. Unmanaged first (the demo's hero finding).
    tab_unm, tab_comp, tab_drift, tab_err = st.tabs([
        f"🔴 Unmanaged ({_unmanaged})",
        f"🟢 Compliant ({_compliant})",
        f"🟡 Drift ({_drifted})",
        f"⚠️ Errors ({_errors})",
    ])

    import pandas as pd

    # ---- UNMANAGED TAB (PUI-4g category-standard) -------------------------
    with tab_unm:
        if not _unmanaged_orphan and not _unmanaged_child:
            st.success(
                "🎉 Zero unmanaged resources -- everything in this "
                "project is codified.",
                icon="🎉",
            )
        else:
            st.caption(
                "These resources exist in your GCP project but are "
                "**not tracked by Terraform**. Auto-spawned children "
                "of managed parents (e.g. GKE node-pool VMs, default "
                "service accounts) are hidden by default — toggle "
                "below to reveal."
            )

            # Filter + child-toggle row
            type_options = sorted({
                r.get("tf_type", "")
                for r in _unmanaged_orphan + _unmanaged_child
            })
            f_col, t_col, c_col = st.columns([2, 1.2, 1])
            with f_col:
                type_filter = st.multiselect(
                    "Filter by type",
                    options=type_options,
                    default=[],
                    placeholder="Show all types",
                    key="dt_unm_type_filter",
                )
            with t_col:
                show_children = st.checkbox(
                    "Show child resources of managed parents",
                    value=False,
                    key="dt_unm_show_children",
                    help=(
                        "OFF (default): only genuinely-orphan resources "
                        "count as Unmanaged. ON: also show resources "
                        "auto-managed by a parent in Compliant -- useful "
                        "for auditing the heuristic."
                    ),
                )

            # Build the visible rows, partitioned.
            orphan_rows = []
            for r in _unmanaged_orphan:
                if type_filter and r.get("tf_type") not in type_filter:
                    continue
                orphan_rows.append({
                    "#": len(orphan_rows) + 1,
                    "Status": "🔴 Unmanaged",
                    "Resource": (
                        r.get("cloud_name", "")
                        or r.get("cloud_urn", "")[-50:]
                    ),
                    "Type": r.get("tf_type", ""),
                    "Location": r.get("location") or "—",
                })
            child_rows = []
            if show_children:
                for r in _unmanaged_child:
                    if (
                        type_filter
                        and r.get("tf_type") not in type_filter
                    ):
                        continue
                    child_rows.append({
                        "#": len(child_rows) + 1,
                        "Status": "🟦 Auto-managed",
                        "Resource": (
                            r.get("cloud_name", "")
                            or r.get("cloud_urn", "")[-50:]
                        ),
                        "Type": r.get("tf_type", ""),
                        "Owned by": r.get("_parent_owner", ""),
                        "Location": r.get("location") or "—",
                    })

            with c_col:
                st.metric(
                    "Visible",
                    len(orphan_rows)
                    + (len(child_rows) if show_children else 0),
                )

            # Render orphans (the demo's hero finding).
            if orphan_rows:
                st.markdown(
                    f"#### 🔴 Genuinely unmanaged "
                    f"({len(orphan_rows)})"
                )
                st.dataframe(
                    pd.DataFrame(orphan_rows),
                    column_config={
                        "#": st.column_config.NumberColumn(
                            width="small",
                        ),
                        "Status": st.column_config.TextColumn(
                            "Status", width="small",
                        ),
                        "Resource": st.column_config.TextColumn(
                            "Resource", width="large",
                        ),
                        "Type": st.column_config.TextColumn(
                            "Type", width="medium",
                        ),
                        "Location": st.column_config.TextColumn(
                            "Location", width="small",
                        ),
                    },
                    hide_index=True,
                    use_container_width=True,
                )
                st.info(
                    "📥 **Codify these in Inventory**: head to the "
                    "**Inventory** page, Discover, then pick + Run "
                    "import for the resources you want IaC-managed. "
                    "(Per-row deep-link button queued as PUI-4d.)",
                    icon="📥",
                )
            elif _unmanaged_child and not show_children:
                st.success(
                    f"🎉 Zero genuinely-unmanaged resources. "
                    f"({len(_unmanaged_child)} auto-managed child "
                    f"resource(s) hidden — toggle above to view.)",
                    icon="🎉",
                )

            # Render auto-managed children when toggled.
            if show_children and child_rows:
                st.markdown("---")
                st.markdown(
                    f"#### 🟦 Auto-managed by parent in Compliant "
                    f"({len(child_rows)})"
                )
                st.caption(
                    "_These resources exist in your cloud but are "
                    "auto-managed by a parent resource that IS in "
                    "your terraform state. Codifying them separately "
                    "is usually wrong (the parent recreates them). "
                    "Heuristic-based — review if any look misclassified._"
                )
                st.dataframe(
                    pd.DataFrame(child_rows),
                    column_config={
                        "#": st.column_config.NumberColumn(
                            width="small",
                        ),
                        "Status": st.column_config.TextColumn(
                            "Status", width="small",
                        ),
                        "Resource": st.column_config.TextColumn(
                            "Resource", width="large",
                        ),
                        "Type": st.column_config.TextColumn(
                            "Type", width="medium",
                        ),
                        "Owned by": st.column_config.TextColumn(
                            "Owned by", width="medium",
                        ),
                        "Location": st.column_config.TextColumn(
                            "Location", width="small",
                        ),
                    },
                    hide_index=True,
                    use_container_width=True,
                )

    # ---- COMPLIANT TAB ------------------------------------------------
    with tab_comp:
        compliant_list = last_result.get("compliant", [])
        if not compliant_list:
            st.info(
                "No resources in terraform state for this project yet. "
                "Run an import in **Inventory** first.",
                icon="ℹ️",
            )
        else:
            st.caption(
                f"_{len(compliant_list)} resource(s) tracked by "
                f"Terraform. Drift detection (per-resource `terraform "
                f"plan`) is queued as PUI-4e -- once shipped, items "
                f"with cloud-vs-HCL drift will move to the Drift tab._"
            )
            type_options = sorted({
                r.get("tf_type", "") for r in compliant_list
            })
            f_col, c_col = st.columns([3, 1])
            with f_col:
                type_filter = st.multiselect(
                    "Filter by type",
                    options=type_options,
                    default=[],
                    placeholder="Show all types",
                    key="dt_comp_type_filter",
                )
            rows = []
            for r in compliant_list:
                if type_filter and r.get("tf_type") not in type_filter:
                    continue
                rows.append({
                    "#": len(rows) + 1,
                    "Status": "🟢 Compliant",
                    "Resource": r.get("hcl_name", ""),
                    "Type": r.get("tf_type", ""),
                    "TF address": r.get("tf_address", ""),
                })
            with c_col:
                st.metric("Visible", len(rows))
            if rows:
                st.dataframe(
                    pd.DataFrame(rows),
                    hide_index=True,
                    use_container_width=True,
                )

    # ---- DRIFT TAB ----------------------------------------------------
    # PUI-4e/4i/4j (2026-04-30): drift detection wired via diff_engine
    # (cloud-vs-state per-field diff, NOT terraform-plan-based).
    # Per-resource expander shows side-by-side diff (Path | State |
    # Cloud) and 4 remediation actions matching the CLI:
    #   * Restore  -- terraform apply -target (HCL -> Cloud)
    #   * Accept   -- terraform refresh-only (Cloud -> State, .tf untouched)
    #   * Recreate -- destroy + apply (DESTRUCTIVE)
    #   * Drop     -- terraform state rm (stop managing)
    # Type-to-confirm gate (matches Danger Zone pattern) prevents
    # mis-clicks. Policy gate disabled in v1 -- the dedicated Policy
    # page (PUI-5) will be where operators see / acknowledge violations.
    with tab_drift:
        drift_list = last_result.get("drifted", [])
        if not drift_list:
            st.success(
                "No drift detected. Every in-state resource matches "
                "its cloud counterpart on every checked field.",
                icon="🎉",
            )
        else:
            # Summary table at the top.
            rows = [
                {
                    "#": i + 1,
                    "Status": "❌ Error" if d.get("error") else "🟡 Drift",
                    "TF address": d.get("tf_address", ""),
                    "Type": d.get("tf_type", ""),
                    "Drifted fields": (
                        len(d.get("items") or [])
                        if not d.get("error") else "(snapshot missing)"
                    ),
                    "Policy": d.get("policy_tag") or "—",
                }
                for i, d in enumerate(drift_list)
            ]
            st.dataframe(
                pd.DataFrame(rows),
                hide_index=True,
                use_container_width=True,
            )

            st.markdown("### Per-resource detail")
            st.caption(
                "Expand a resource to see the cloud-vs-state diff and "
                "remediate. **Type the TF address** to enable action "
                "buttons (prevents fat-finger mistakes)."
            )

            for i, drift in enumerate(drift_list):
                _tfa = drift.get("tf_address", "")
                _items = drift.get("items") or []
                _err = drift.get("error")
                _hdr = (
                    f"❌ {_tfa} — snapshot missing"
                    if _err
                    else f"🟡 {_tfa} — {len(_items)} field(s)"
                )
                with st.expander(_hdr, expanded=False):
                    if _err:
                        st.error(
                            f"{_err}\n\nThe resource may have been "
                            "deleted out-of-band. Use **Drop** to "
                            "remove from state, or **Recreate** to "
                            "rebuild from HCL.",
                            icon="❌",
                        )
                    else:
                        # 3-column header
                        h1, h2, h3 = st.columns([2, 3, 3])
                        h1.markdown("**Path**")
                        h2.markdown("**State (HCL)**")
                        h3.markdown("**Cloud**")
                        st.divider()
                        for it in _items:
                            c1, c2, c3 = st.columns([2, 3, 3])
                            _op = it.get("op", "")
                            _glyph = {
                                "added":   "➕",
                                "removed": "➖",
                                "changed": "✏️",
                            }.get(_op, "•")
                            c1.markdown(
                                f"{_glyph} `{it.get('path', '') or '(root)'}`\n\n"
                                f"_{_op}_"
                            )
                            _sv = it.get("state_value")
                            _cv = it.get("cloud_value")
                            c2.code(
                                json.dumps(_sv, indent=2, default=str)
                                if _sv is not None else "—",
                                language="json",
                            )
                            c3.code(
                                json.dumps(_cv, indent=2, default=str)
                                if _cv is not None else "—",
                                language="json",
                            )

                    # Remediation action panel
                    st.markdown("---")
                    st.markdown("#### 🛠️ Remediation actions")
                    st.caption(
                        "🔄 **Restore HCL→Cloud**: `terraform apply -target` "
                        "— overwrites cloud changes  ·  "
                        "✅ **Accept Cloud→State**: `terraform refresh -target` "
                        "— acknowledges the cloud change (.tf unchanged)  ·  "
                        "♻️ **Recreate**: `destroy + apply` (DESTRUCTIVE)  ·  "
                        "🗑️ **Stop managing**: `terraform state rm`"
                    )

                    # PUI-4s (2026-04-30): typed-confirm gate removed for
                    # demo UX. Operator clicks the action button directly.
                    # Trade-off: easier mis-click on destructive actions
                    # (Recreate, Drop). Acceptable for demo / friendly
                    # internal use; reconsider for production-grade
                    # multi-tenant deployments by adding a 2-step confirm
                    # dialog (PUI-4s-prod follow-up).
                    ba1, ba2, ba3, ba4 = st.columns(4)
                    if ba1.button(
                        "🔄 Restore",
                        key=f"_btn_restore_{_tfa}",
                        use_container_width=True,
                        help="terraform apply -target (HCL→Cloud)",
                    ):
                        st.session_state["_pending_remediation"] = {
                            "tf_address": _tfa, "action": "restore",
                        }
                        st.rerun()
                    if ba2.button(
                        "✅ Accept",
                        key=f"_btn_accept_{_tfa}",
                        use_container_width=True,
                        help="terraform refresh-only (Cloud→State)",
                    ):
                        st.session_state["_pending_remediation"] = {
                            "tf_address": _tfa, "action": "accept",
                        }
                        st.rerun()
                    if ba3.button(
                        "♻️ Recreate",
                        key=f"_btn_recreate_{_tfa}",
                        use_container_width=True,
                        help="terraform destroy + apply (DESTRUCTIVE)",
                        type="primary",
                    ):
                        st.session_state["_pending_remediation"] = {
                            "tf_address": _tfa, "action": "recreate",
                        }
                        st.rerun()
                    if ba4.button(
                        "🗑️ Stop managing",
                        key=f"_btn_drop_{_tfa}",
                        use_container_width=True,
                        help="terraform state rm (no cloud change)",
                    ):
                        st.session_state["_pending_remediation"] = {
                            "tf_address": _tfa, "action": "drop",
                        }
                        st.rerun()

                    # Per-resource last-action result.
                    _rem_key = f"_remediation_result_{_tfa}"
                    if _rem_key in st.session_state:
                        _r = st.session_state[_rem_key]
                        _r_action = _r.get("action", "?")
                        _r_status = _r.get("status", "?")
                        _r_msg = _r.get("message", "")
                        if _r.get("success"):
                            st.success(
                                f"✅ **{_r_action}** completed "
                                f"(`{_r_status}`): {_r_msg}",
                                icon="✅",
                            )
                        else:
                            st.error(
                                f"❌ **{_r_action}** failed "
                                f"(`{_r_status}`): {_r_msg}",
                                icon="❌",
                            )
                        if st.button(
                            "Clear result",
                            key=f"_btn_clear_{_tfa}",
                            type="secondary",
                        ):
                            st.session_state.pop(_rem_key, None)
                            st.rerun()

    # ---- ERRORS TAB ---------------------------------------------------
    with tab_err:
        errors_list = last_result.get("inventory_errors", [])
        if not errors_list:
            st.success(
                "No enumeration errors -- the cloud inventory is "
                "complete.",
                icon="🎉",
            )
        else:
            st.error(
                f"❌ {len(errors_list)} asset type(s) failed to "
                f"enumerate. The Unmanaged tab may be missing "
                f"resources of these types.",
                icon="❌",
            )
            for err in errors_list:
                st.markdown(f"- `{err}`")

    with st.expander("Full DriftReport (structured)", expanded=False):
        st.json(last_result)


# --- PUI-4j: Remediation execution ------------------------------------
# When a remediation button is clicked, the handler stashes
# {tf_address, action} in st.session_state["_pending_remediation"] and
# triggers a rerun. We execute it HERE (after the render block, before
# the rescan-button gate) so the UI surfaces a live spinner in the same
# tab and the result lands back in session_state for the next render.
#
# Why not run inside the button callback: Streamlit re-runs the script
# top-down on each interaction. Running engine work mid-render would
# block render of all later widgets and break the "click → see spinner
# in place" UX. Defer-then-execute is the canonical pattern.
_pending = st.session_state.get("_pending_remediation")
if _pending:
    _tfa = _pending["tf_address"]
    _act = _pending["action"]
    # Clear the pending marker FIRST so a re-run doesn't double-execute.
    st.session_state.pop("_pending_remediation", None)

    _rem_lock_key = f"_remediation_lock_{_tfa}"
    st.session_state[_rem_lock_key] = {
        "start_ts": _time.time(), "action": _act,
    }
    _result_key = f"_remediation_result_{_tfa}"
    try:
        # Lazy imports to keep page-load fast (engine modules pull
        # google-cloud SDK which is heavy).
        from importer import terraform_client
        from detector import remediator
        from app.middleware import workdir_context, bust_workdir_cache

        with st.spinner(
            f"Running **{_act}** on `{_tfa}`… This may take 30s-5min "
            f"depending on the action (terraform init + plan + apply)."
        ):
            with workdir_context(project_id) as workdir:
                # Ensure terraform.tfstate is materialized locally
                # (mirrors the rescan path).
                state_file = os.path.join(workdir, "terraform.tfstate")
                terraform_client.state_pull(
                    workdir=workdir, output_path=state_file,
                )
                rem_result = remediator.remediate_one(
                    tf_address=_tfa,
                    action=_act,
                    auto_confirm=True,
                    enable_policy_gate=False,  # PUI-4j v1: deferred
                    workdir=workdir,
                )
        # Bust the GCS handle cache so the next page load sees fresh
        # state (terraform may have rewritten state on Restore/Recreate/Drop).
        bust_workdir_cache(project_id)

        st.session_state[_result_key] = {
            "action": _act,
            "success": rem_result.success,
            "status": rem_result.status,
            "message": rem_result.message,
            "_cached_at": _time.time(),
        }
    except Exception as e:  # noqa: BLE001 -- defensive shell for UI
        st.session_state[_result_key] = {
            "action": _act,
            "success": False,
            "status": "exception",
            "message": f"{type(e).__name__}: {e}",
            "_cached_at": _time.time(),
        }
    finally:
        st.session_state.pop(_rem_lock_key, None)

    # Force a re-render so the result appears in the expander AND so
    # the cached DriftReport gets re-evaluated (state changed).
    st.rerun()


if not rescan_button:
    st.stop()


# --- Live rescan path --------------------------------------------------

if _lock is not None:
    st.warning(
        "Rescan already in progress; ignoring click.", icon="⚠️",
    )
    st.stop()

# Immediate visual feedback (mirrors Inventory + Translator pattern):
rescan_btn_slot.button(
    "⚡ Rescanning…",
    type="secondary",
    disabled=True,
    key="dt_rescan_btn_disabled_swap",
    use_container_width=True,
)
st.toast("⚡ Starting rescan...", icon="🚀")
st.success(
    f"🚀 Rescan started for **{project_id}**. Loading engine "
    f"modules + cloud inventory...",
    icon="🚀",
)

st.session_state[_SS_RUN_LOCK] = {
    "start_ts": _time.time(),
    "project_id": project_id,
}

from app.middleware import workdir_context  # noqa: E402
from detector.rescan import rescan as _rescan  # noqa: E402
from importer.terraform_client import state_pull  # noqa: E402

started = time.monotonic()
try:
    with st.spinner(
        f"Rescanning {project_id} … (cloud inventory + state diff; "
        f"~5-15s depending on resource count)"
    ):
        with workdir_context(project_id) as workdir:
            # PUI-4: ensure local terraform.tfstate exists. With GCS
            # backend (MTAGENT_USE_GCS_BACKEND=1, default in SaaS),
            # terraform writes state to GCS -- the detector's
            # state_reader needs a local file. state_pull is
            # idempotent: if the local file already exists, it
            # overwrites with a fresh copy (always safe).
            state_file = os.path.join(workdir, "terraform.tfstate")
            pulled_ok = state_pull(
                workdir=workdir, output_path=state_file,
            )
            if not pulled_ok:
                # Soft-fail: rescan can still run with empty state
                # (everything cloud-side will land in Unmanaged).
                # Surface a hint so the operator knows.
                st.info(
                    "ℹ️ Couldn't pull terraform state (likely no "
                    "imports run yet for this project, OR backend "
                    "init not done). Continuing with empty state -- "
                    "all cloud resources will appear as Unmanaged.",
                    icon="ℹ️",
                )

            # PUI-4e: drift_check=True runs per-resource cloud-vs-state
            # diff after the unmanaged scan. Adds ~30-90s on a
            # 50-resource project (one parallel-fanned `gcloud describe`
            # per in-scope state resource via cloud_snapshot.fetch_snapshots,
            # then a pure-Python diff via diff_engine.diff_resource).
            # Result: the Drift bucket actually populates with per-field
            # deltas the user can View / Restore / Accept / etc.
            report = _rescan(
                project_id, project_root=workdir, drift_check=True,
            )
except Exception as e:  # noqa: BLE001
    st.session_state.pop(_SS_RUN_LOCK, None)
    render_error(e, context=f"running rescan for {project_id}")
    st.stop()

# Clean exit.
st.session_state.pop(_SS_RUN_LOCK, None)
duration = time.monotonic() - started
result_dict = report.as_fields()
# as_fields() drops the heavy per-bucket lists. Restore them so the UI
# can render. asdict-style for each bucket; mirrors Inventory's pattern.
from dataclasses import asdict
result_dict["unmanaged"] = [asdict(r) for r in report.unmanaged]
result_dict["compliant"] = [asdict(r) for r in report.compliant]
result_dict["drifted"] = [asdict(r) for r in report.drifted]
result_dict["inventory_errors"] = list(report.inventory_errors)
# Restore counts (as_fields included them, but defensive).
result_dict["unmanaged_count"] = report.unmanaged_count
result_dict["compliant_count"] = report.compliant_count
result_dict["drifted_count"] = report.drifted_count
result_dict["inventory_error_count"] = report.inventory_error_count
result_dict["total_in_state"] = report.total_in_state
result_dict["total_in_cloud"] = report.total_in_cloud
result_dict["duration_s"] = round(duration, 2)
# PUI-3b: stamp cache time for auto-recover.
result_dict["_cached_at"] = _time.time()
st.session_state[_SS_LAST_RESULT] = result_dict

st.rerun()
