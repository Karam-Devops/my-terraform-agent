# app/pages/4_Policy.py
"""Policy page -- Streamlit wrapper around policy.scan.scan().

PUI-5b2 (2026-04-30): the SaaS surface for the Policy compliance
engine. Mirrors detector.rescan -> Detector page split:

  * policy.scan.scan(project_id, project_root) -> PolicyReport
    is the engine wrapper (see policy/scan.py).
  * This page is the rendering / interaction layer.

Engine wiring:
  * policy.scan.scan() returns a PolicyReport with per-resource
    Violation lists + severity rollups + cap_hit flag.
  * importer.terraform_client.state_pull() materializes the GCS-
    backend state to a local file before the scan reads it (same
    pattern as Detector page).
  * common.snapshots.write_snapshot already wired inside scan()
    (Dashboard page reads from there in PUI-2).

Defenses preserved (PUI-5b1 audit):
  D1  conftest binary check     -> raise RuntimeError separately,
                                   render admin banner with install hint
  D2  per-resource 30s timeout  -> engine-side, untouched
  D3  per-run 1000-vio cap      -> render truncation banner if cap_hit
  D4  subprocess fail-open      -> engine-side, untouched
  D5  json decode fail-open     -> engine-side, untouched
  D6  conftest engine fail-open -> engine-side, untouched
  D7  missing snapshot -> LOW   -> rendered in Errors tab
  D8  snapshot write best-effort-> engine-side, untouched
  D9  in-scope filter           -> engine-side, untouched
  D10 path windows fallback     -> engine-side, untouched
  D11 detector decoration       -> n/a (this is the policy page)

SaaS-specific defenses (added here):
  D12 Tier-A run lock           -> _SS_RUN_LOCK
  D13 PUI-3b auto-recover       -> stale-lock check via _cached_at
  D14 workdir_context           -> hydrate / persist via middleware
  D15 state_pull before scan    -> required for state read
  D16 bust_workdir_cache        -> after scan to invalidate cache
  D17 render_error catch-all    -> any unexpected exception
  D18 empty-result handling     -> celebratory "no findings" UI
  D19 conftest-missing banner   -> dedicated UI, not generic error
  D20 cap-hit truncation banner -> mandatory if cap_hit=True

Theme: same Firefly DARK polish as Inventory + Translator + Detector.
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
    page_title="mtagent · Policy",
    page_icon="🛡️",
    layout="wide",
)

apply_theme_polish()

project_id = render_sidebar()

st.title("🛡️ Policy")
st.caption(
    "Evaluate every codified resource against the vendored OPA / Rego "
    "policy bundle. Surfaces HIGH / MED / LOW compliance violations + "
    "the cloud snapshot that triggered each finding. Cheap (~5-30s "
    "for a 50-resource project; no LLM cost; conftest binary required)."
)

if not project_id:
    st.warning("Pick a project in the sidebar to get started.", icon="⚠️")
    st.stop()

st.markdown(f"**Project:** `{project_id}`")

# --- Session-state keys -------------------------------------------------
_SS_RUN_LOCK = "_policy_run_lock"
_SS_LAST_RESULT = f"_policy_last_result_{project_id}"


# --- Tier-A run lock + PUI-3b auto-recover -----------------------------
import time as _time

_RUN_TIMEOUT_S = 600  # generous for cap-1000 projects (~30s/resource worst case)
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
        # Cache time newer than lock start = the scan completed
        # successfully but the rerun never reached the browser.
        print(
            f"PUI-3b auto-recover: clearing stale Policy run-lock "
            f"({int(_elapsed)}s old; result cached "
            f"{int(_time.time() - _last_result_for_recover['_cached_at'])}s "
            f"ago)."
        )
        st.session_state.pop(_SS_RUN_LOCK, None)
        _lock = None


# --- Danger Zone (always visible) --------------------------------------
# Policy scan is read-only on cloud + state, so Reset is purely a
# session-state cache clear. Cloud / state untouched.
def _render_danger_zone() -> None:
    with st.expander("⚠️ Danger zone", expanded=False):
        st.markdown(
            f"### Reset scan results for `{project_id}`\n\n"
            f"This clears the cached PolicyReport from this Streamlit "
            f"session. Cloud + terraform state are NOT touched -- "
            f"policy scan is purely read-only.\n\n"
            f"_Use when you want to redo a scan from scratch (e.g., "
            f"after editing the Rego rules or adding a new resource "
            f"via Inventory)._"
        )
        typed_confirm = st.text_input(
            f"Type the project ID to confirm: `{project_id}`",
            value="",
            key="dz_pol_confirm",
            placeholder=project_id,
        )
        confirm_match = typed_confirm.strip() == project_id
        reset_btn_disabled = (
            (not confirm_match) or (_lock is not None)
        )
        reset_help = (
            "Type the project ID exactly to enable this button."
            if not confirm_match
            else "Scan in progress; wait for it to complete."
            if _lock is not None
            else "Clears the cached PolicyReport. Cloud + state untouched."
        )
        if st.button(
            "🗑️ Reset scan results",
            type="primary",
            disabled=reset_btn_disabled,
            key="dz_pol_reset_btn",
            help=reset_help,
        ):
            st.session_state.pop(_SS_LAST_RESULT, None)
            st.success(
                f"✅ Scan results cleared for `{project_id}`. "
                f"Click Run scan above for a fresh scan.",
                icon="✅",
            )


_render_danger_zone()


# --- Run scan trigger --------------------------------------------------
st.markdown("---")
st.markdown("### Run scan")
st.caption(
    "Reads `terraform.tfstate`, fetches a live cloud snapshot per "
    "in-scope resource, and evaluates each against the vendored Rego "
    "policy bundle. Findings grouped by severity. ~5-30s per scan."
)

run_col, info_col = st.columns([1, 2])
with run_col:
    scan_btn_slot = st.empty()
    scan_button = scan_btn_slot.button(
        "▶ Run scan" if not _lock
        else f"Scanning ({int(_time.time() - _lock['start_ts'])}s)…",
        type="primary",
        disabled=(_lock is not None),
        key="pol_scan_btn",
        use_container_width=True,
    )
with info_col:
    if _lock is not None:
        st.warning(
            f"⏳ Scan in progress for **{_lock.get('project_id')}**; "
            f"started {int(_time.time() - _lock['start_ts'])}s ago.",
            icon="⏳",
        )
    elif _last_result_for_recover is not None:
        _last_dur = _last_result_for_recover.get("duration_s", 0)
        st.caption(
            f"_Last scan: {_last_dur:.1f}s. Click **Run scan** to "
            f"refresh._"
        )
    else:
        st.caption(
            "_No scan run yet for this project in this session. "
            "Click **Run scan** above._"
        )


# --- Render last PolicyReport (if present) -----------------------------

last_result = st.session_state.get(_SS_LAST_RESULT)

if last_result and not scan_button:
    st.markdown("---")

    # Conftest-missing dedicated banner (PUI-5b2 D19). When the engine
    # raised RuntimeError the scan path stores a synthetic result with
    # _conftest_missing=True; render the install-hint UI instead of
    # the normal report.
    if last_result.get("_conftest_missing"):
        st.error(
            "❌ **conftest binary is missing.** The Policy engine "
            "needs `conftest` on PATH to evaluate Rego rules. "
            "**Admin action required:** rebuild the Cloud Run image "
            "with conftest installed.",
            icon="❌",
        )
        with st.expander("Install instructions (engine output)", expanded=True):
            st.code(
                last_result.get("_conftest_install_hint", ""),
                language="bash",
            )
        st.stop()

    _total = last_result.get("total_violations", 0)
    _high = last_result.get("high_count", 0)
    _med = last_result.get("med_count", 0)
    _low = last_result.get("low_count", 0)
    _n_resources = last_result.get("n_resources", 0)
    _compliant_resources = last_result.get("compliant_resources", 0)
    _violating = last_result.get("violating_resources", 0)
    _cap_hit = last_result.get("cap_hit", False)

    # PUI-5b2 D20: cap-hit truncation banner. MANDATORY when cap_hit.
    if _cap_hit:
        st.warning(
            f"⚠️ **Truncated** at {_total} violation(s). The per-run "
            f"cap (`policy.config.MAX_VIOLATIONS_PER_RUN`) was reached "
            f"-- subsequent resources / violations were NOT evaluated. "
            f"This usually indicates a buggy rule, malicious input, "
            f"or an unusually large project. Some findings may be "
            f"missing from the lists below.",
            icon="⚠️",
        )

    # Compliance score gauge (Firefly's hero metric).
    if _n_resources > 0:
        _compliance_pct = round(100.0 * _compliant_resources / _n_resources)
    else:
        _compliance_pct = 100  # vacuously compliant
    st.markdown(f"### Compliance: **{_compliance_pct}%** of resources passing")
    st.progress(
        min(_compliant_resources, _n_resources) / max(_n_resources, 1),
        text=(
            f"{_compliant_resources} of {_n_resources} in-scope resource(s) "
            f"passing all policies"
            + (" (out of scope: not shown)" if _n_resources == 0 else "")
        ),
    )

    # 4-metric grid.
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("📊 Total", _total)
    m2.metric("🔴 HIGH", _high, delta=("CI fail" if _high else None),
              delta_color="inverse")
    m3.metric("🟡 MED", _med)
    m4.metric("🔵 LOW", _low)

    if _n_resources == 0:
        st.info(
            "No in-scope resources to evaluate. Either no resources "
            "have been imported yet (run **Inventory**) or none of "
            "the imported types are covered by the policy enforcer's "
            "in-scope set. Policy scan ran successfully but had "
            "nothing to check.",
            icon="ℹ️",
        )
    elif _total == 0:
        # PUI-5b2 D18: empty-result celebration UI.
        st.success(
            f"🎉 **All {_n_resources} in-scope resource(s) passing "
            f"every applicable policy.** No HIGH / MED / LOW "
            f"violations across the project.",
            icon="🎉",
        )

    # ---- Tabbed bucket view ------------------------------------------
    tab_high, tab_med, tab_low, tab_err = st.tabs([
        f"🔴 HIGH ({_high})",
        f"🟡 MED ({_med})",
        f"🔵 LOW ({_low})",
        "⚠️ Errors",
    ])

    # Pre-build per-severity lists for the tabs. Each entry is
    # (tf_address, violation_dict). We iterate per_resource so the
    # ordering by tf_address is deterministic.
    _per_resource = last_result.get("per_resource", {}) or {}
    _by_severity = {"HIGH": [], "MED": [], "LOW": []}
    _errors = []  # cloud_snapshot_missing rule_id, surfaced separately
    for _addr in sorted(_per_resource.keys()):
        for _v in _per_resource[_addr]:
            _sev = _v.get("severity", "")
            _rule = _v.get("rule_id", "")
            # Errors tab: missing-snapshot LOWs (D7) + future
            # synthesized engine-error finding types.
            if _rule == "cloud_snapshot_missing":
                _errors.append((_addr, _v))
                continue
            if _sev in _by_severity:
                _by_severity[_sev].append((_addr, _v))

    def _render_violation_table(items: list, sev_color: str) -> None:
        """Render violations as a sortable table + per-row expander."""
        if not items:
            st.success(
                f"No {sev_color} violations.",
                icon="✅",
            )
            return
        # Summary table at top.
        import pandas as pd
        rows = [
            {
                "#": i + 1,
                "Resource": addr,
                "Rule": v.get("rule_id", ""),
                "Message": (v.get("message", "") or "")[:80],
            }
            for i, (addr, v) in enumerate(items)
        ]
        st.dataframe(
            pd.DataFrame(rows),
            hide_index=True,
            use_container_width=True,
        )
        st.markdown("##### Per-violation detail")
        for i, (addr, v) in enumerate(items):
            with st.expander(
                f"{sev_color} `{addr}` — {v.get('rule_id', '')}: "
                f"{v.get('message', '')}",
                expanded=False,
            ):
                # Policy file path.
                pf = v.get("policy_file", "") or "(unknown)"
                if pf in ("(unknown)", "(infrastructure)"):
                    st.caption(f"Policy source: `{pf}` (sentinel)")
                else:
                    try:
                        rel = os.path.relpath(pf)
                    except ValueError:
                        # Cross-drive on Windows; fall back to absolute.
                        rel = pf
                    st.caption(f"Policy source: `{rel}`")
                st.markdown(f"**Severity:** `{v.get('severity', '')}`  ·  "
                            f"**Rule:** `{v.get('rule_id', '')}`")
                st.markdown(f"**Message:**")
                st.code(v.get("message", ""), language="text")
                st.markdown(f"**TF address:** `{addr}`")

    with tab_high:
        if _by_severity["HIGH"]:
            st.error(
                f"❌ {_high} HIGH violation(s) — these block CI "
                f"(`exit_code != 0`). Fix or explicitly accept.",
                icon="❌",
            )
        _render_violation_table(_by_severity["HIGH"], "🔴")

    with tab_med:
        _render_violation_table(_by_severity["MED"], "🟡")

    with tab_low:
        _render_violation_table(_by_severity["LOW"], "🔵")

    with tab_err:
        # PUI-5b2 D7-render: missing-snapshot LOWs go here, NOT in the
        # severity tabs (they're not real policy findings, they're
        # observability gaps).
        if not _errors:
            st.success(
                "No engine errors -- every in-scope resource was "
                "evaluated successfully.",
                icon="🎉",
            )
        else:
            st.warning(
                f"⚠️ {len(_errors)} resource(s) couldn't be evaluated "
                f"(cloud snapshot unavailable). The Detector is the "
                f"right surface to investigate these -- they may have "
                f"been deleted out-of-band.",
                icon="⚠️",
            )
            for addr, v in _errors:
                st.markdown(f"- `{addr}`: {v.get('message', '')}")

    with st.expander("Full PolicyReport (structured)", expanded=False):
        st.json(last_result)


if not scan_button:
    st.stop()


# --- Live scan path ----------------------------------------------------

if _lock is not None:
    st.warning(
        "Scan already in progress; ignoring click.", icon="⚠️",
    )
    st.stop()

# Immediate visual feedback (mirrors Inventory + Translator + Detector
# pattern): swap button to disabled + "Scanning…" before subprocess
# starts so the operator can't double-click.
scan_btn_slot.button(
    "⚡ Scanning…",
    type="secondary",
    disabled=True,
    key="pol_scan_btn_disabled_swap",
    use_container_width=True,
)

# Acquire the run lock + stamp start time for PUI-3b stale-lock recover.
st.session_state[_SS_RUN_LOCK] = {
    "project_id": project_id,
    "start_ts": _time.time(),
}

started = time.monotonic()

try:
    # Lazy imports keep page-load fast (engine modules pull google-cloud
    # SDK which is heavy).
    from importer import terraform_client
    from policy.scan import scan as _policy_scan
    from app.middleware import workdir_context, bust_workdir_cache

    with st.spinner(
        f"Running Policy scan on **{project_id}**… (~5-30s; conftest "
        f"runs once per resource against the Rego bundle)"
    ):
        with workdir_context(project_id) as workdir:
            # PUI-5b2 D15: state_pull required so policy.scan can read
            # local terraform.tfstate. Same dance as Detector page.
            state_file = os.path.join(workdir, "terraform.tfstate")
            pulled_ok = terraform_client.state_pull(
                workdir=workdir, output_path=state_file,
            )
            if not pulled_ok:
                # Soft-fail: scan can still run but will report "no
                # in-scope resources" (state empty). Surface the hint.
                st.info(
                    "ℹ️ Couldn't pull terraform state (likely no "
                    "imports run yet for this project, OR backend "
                    "init not done). Scan will report 0 in-scope "
                    "resources -- run the Inventory page first.",
                    icon="ℹ️",
                )

            # PUI-5b2 D19: conftest-missing handled separately so the
            # UI can render an admin-actionable banner (install hint)
            # instead of a generic engine error.
            try:
                report = _policy_scan(project_id, project_root=workdir)
            except RuntimeError as conftest_err:
                # The engine's ensure_conftest_available() raises this.
                # Stash a synthetic result so the renderer shows the
                # install hint UI; the scan didn't run, so nothing
                # to persist.
                st.session_state.pop(_SS_RUN_LOCK, None)
                st.session_state[_SS_LAST_RESULT] = {
                    "_conftest_missing": True,
                    "_conftest_install_hint": str(conftest_err),
                    "_cached_at": _time.time(),
                }
                st.rerun()
except Exception as e:  # noqa: BLE001 -- defensive shell for UI
    # PUI-5b2 D17: any unexpected engine failure renders friendly
    # error UI. Lock cleared so retry is possible.
    st.session_state.pop(_SS_RUN_LOCK, None)
    render_error(e, context=f"running policy scan for {project_id}")
    st.stop()

# Clean exit. Bust workdir cache so any subsequent page (Detector,
# Inventory) sees fresh state if the user hops between tabs.
try:
    bust_workdir_cache(project_id)  # PUI-5b2 D16
except Exception:  # noqa: BLE001 -- best-effort
    pass

st.session_state.pop(_SS_RUN_LOCK, None)
duration = time.monotonic() - started

# Build the result dict. as_fields() drops the heavy per_resource map;
# restore it so the UI can render the per-violation expanders.
from dataclasses import asdict
result_dict = report.as_fields()
result_dict["per_resource"] = {
    addr: [asdict(v) for v in violations]
    for addr, violations in report.per_resource.items()
}
result_dict["duration_s"] = round(duration, 2)
# PUI-3b: stamp cache time for auto-recover.
result_dict["_cached_at"] = _time.time()
st.session_state[_SS_LAST_RESULT] = result_dict

st.rerun()
