# app/pages/5_📊_Dashboard.py
"""Dashboard page (PUI-2) -- industry-parity hero overview.

Reads per-engine snapshots from GCS (PSA-9 + PUI-2pre) and renders:

  * **Hero strip** (4 metrics): Coverage % / Compliance % / Drift /
    Total Managed.
  * **4 engine cards** in a 2x2 grid: Inventory / Cross-Cloud
    Translation / Drift Detection & Remediation / Policy as Code.
    Each card shows the last-run timestamp + headline metrics +
    a link back to the engine's page.
  * **Recent activity feed**: last 10 snapshots across all 4
    engines, ordered by timestamp desc.

Engine wiring:
  * common.snapshots.read_latest_snapshot(engine, project_id) ->
    envelope dict {engine, written_at, tenant_id, project_id, data}
  * common.snapshots.list_history(engine, project_id, limit=10) ->
    list of {timestamp, gs_uri, size_bytes}
  * common.snapshots.read_history_entry(gs_uri) -> envelope dict
    (used for the activity feed when we want headline metric per row)

Defenses:
  * Cache reads via @st.cache_data(ttl=60) so back-to-back renders
    don't refetch.
  * Empty-state cards (no snapshot yet) show "No data -- run X" with
    a page_link to the engine.
  * Refresh button clears the cache.
  * Pure read; no engine subprocess, no LLM, no spinner risk.
  * GCS read failures already swallowed inside snapshots.py
    (returns None / [] -> empty-state UX).

Theme: same dark theme polish as the other pages.
"""

from __future__ import annotations

import time as _time
from datetime import datetime, timezone
from typing import Optional

import altair as alt
import pandas as pd
import streamlit as st

from app.ui.sidebar import render_sidebar
from app.ui.theme import apply_theme_polish


# Page chrome
st.set_page_config(
    page_title="mtagent · Dashboard",
    page_icon="📊",
    layout="wide",
)

apply_theme_polish()

project_id = render_sidebar()

st.title("📊 Dashboard")
st.caption(
    "Cross-engine overview. Reads per-engine snapshots from GCS "
    "(no engine re-execution; results are cached for 60s)."
)

if not project_id:
    st.warning("Pick a project in the sidebar to get started.", icon="⚠️")
    st.stop()

st.markdown(f"**Project:** `{project_id}`")


# --- Helpers ----------------------------------------------------------

_ENGINES = ("importer", "translator", "detector", "policy")
_ENGINE_LABELS = {
    "importer":   "📋 Inventory",
    "translator": "🌐 Cross-Cloud Translation",
    "detector":   "🔍 Drift Detection & Remediation",
    "policy":     "🛡️ Policy as Code",
}
# Streamlit page_link targets (filename without extension).
# PUI-5g (2026-04-30): renumbered after Dashboard moved to slot 1.
_ENGINE_PAGE_PATHS = {
    "importer":   "app/pages/2_📋_Inventory.py",
    "translator": "app/pages/3_🌐_Cross_Cloud_Translation.py",
    "detector":   "app/pages/4_🔍_Drift_Detection_and_Remediation.py",
    "policy":     "app/pages/5_🛡️_Policy_as_Code.py",
}


@st.cache_data(ttl=60, show_spinner=False)
def _read_snapshot(engine: str, project: str) -> Optional[dict]:
    """Cached read of latest snapshot for one (engine, project)."""
    from common.snapshots import read_latest_snapshot
    return read_latest_snapshot(engine, project)


@st.cache_data(ttl=60, show_spinner=False)
def _read_history(engine: str, project: str, limit: int = 10) -> list:
    """Cached list of recent snapshots for one (engine, project)."""
    from common.snapshots import list_history
    return list_history(engine, project, limit=limit)


@st.cache_data(ttl=60, show_spinner=False)
def _read_history_entry(gs_uri: str) -> Optional[dict]:
    """Cached read of one history entry by gs:// URI."""
    from common.snapshots import read_history_entry
    return read_history_entry(gs_uri)


def _format_relative_time(written_at: Optional[str]) -> str:
    """Convert ISO-8601 UTC timestamp to '5m ago' / '2h ago' / etc."""
    if not written_at:
        return "n/a"
    try:
        # Our envelope uses YYYY-MM-DDTHH-MM-SSZ (dashes, not colons,
        # in time component to be filesystem-safe). Convert back for parse.
        # First try the dash form, then fall back to colon form.
        ts_str = written_at.rstrip("Z")
        # Try filesystem-safe shape (dashes throughout):
        try:
            dt = datetime.strptime(ts_str, "%Y-%m-%dT%H-%M-%S")
        except ValueError:
            # Fallback: standard ISO with colons.
            dt = datetime.fromisoformat(ts_str)
        dt = dt.replace(tzinfo=timezone.utc)
        delta = datetime.now(timezone.utc) - dt
        secs = int(delta.total_seconds())
        if secs < 60:
            return f"{secs}s ago"
        if secs < 3600:
            return f"{secs // 60}m ago"
        if secs < 86400:
            return f"{secs // 3600}h ago"
        return f"{secs // 86400}d ago"
    except Exception:  # noqa: BLE001 -- shape-tolerant
        return written_at  # fall back to raw string


def _engine_headline(engine: str, data: dict) -> str:
    """Build a one-line headline from an engine's snapshot data dict."""
    if engine == "importer":
        # WorkflowResult.as_fields(): imported, failed, skipped,
        # needs_attention
        imp = data.get("imported", 0)
        sel = data.get("selected", 0)
        na = data.get("needs_attention", 0)
        return f"Imported {imp}/{sel} resources ({na} needs attention)"
    if engine == "translator":
        # TranslationResult.as_fields(): translated, target_cloud,
        # selected, needs_attention
        tr = data.get("translated", 0)
        sel = data.get("selected", 0)
        tgt = data.get("target_cloud", "?")
        return f"Translated {tr}/{sel} to {tgt}"
    if engine == "detector":
        # DriftReport.as_fields(): drifted_count, compliant_count,
        # unmanaged_count + PUI-2pre orphan/coverage fields
        cmp = data.get("compliant_count", 0)
        drf = data.get("drifted_count", 0)
        orphan = data.get("unmanaged_orphan_count", 0)
        cov = data.get("coverage_pct", 0)
        return f"{cov}% coverage · {cmp} compliant · {drf} drift · {orphan} unmanaged"
    if engine == "policy":
        # PolicyReport.as_fields(): high_count, med_count, low_count,
        # total_violations, n_resources, compliant_resources
        h = data.get("high_count", 0)
        m = data.get("med_count", 0)
        l = data.get("low_count", 0)
        n = data.get("n_resources", 0)
        c = data.get("compliant_resources", 0)
        return f"{c}/{n} resources passing · {h} HIGH · {m} MED · {l} LOW"
    return "(unrecognized engine)"


# --- Top: refresh button ---------------------------------------------

ref_col, _spacer = st.columns([1, 4])
with ref_col:
    if st.button("🔄 Refresh", type="secondary", use_container_width=True):
        st.cache_data.clear()
        st.rerun()


# --- Read all 4 engine snapshots (cached) ----------------------------

snapshots = {e: _read_snapshot(e, project_id) for e in _ENGINES}


def _envelope_data(env: Optional[dict]) -> Optional[dict]:
    """Extract the inner data dict from an envelope. PUI-2pre wrapped
    payload as {engine, written_at, tenant_id, project_id, data}.
    Tolerant of pre-PUI-2pre snapshots that were the bare result dict."""
    if env is None:
        return None
    if "data" in env and "engine" in env and "written_at" in env:
        return env.get("data") or {}
    # Pre-PUI-2pre shape: the bare result dict itself.
    return env


def _envelope_written_at(env: Optional[dict]) -> Optional[str]:
    """Extract the written_at timestamp; None for pre-PUI-2pre shapes."""
    if env is None:
        return None
    return env.get("written_at")


# --- Hero strip: 4 metrics -------------------------------------------

st.markdown("---")
st.markdown("### Overview")

# Pull data for hero from detector + policy snapshots.
det_env = snapshots["detector"]
pol_env = snapshots["policy"]
det = _envelope_data(det_env) or {}
pol = _envelope_data(pol_env) or {}

_coverage = det.get("coverage_pct", 0) if det else 0
_drift = det.get("drifted_count", 0) if det else 0
_compliant_resources = pol.get("compliant_resources", 0) if pol else 0
_n_resources_pol = pol.get("n_resources", 0) if pol else 0
_compliance_pct = (
    round(100.0 * _compliant_resources / _n_resources_pol)
    if _n_resources_pol > 0 else 0
)
_total_managed = (det.get("compliant_count", 0) + det.get("drifted_count", 0)) if det else 0

h1, h2, h3, h4 = st.columns(4)
h1.metric(
    "🎯 Coverage",
    f"{_coverage}%" if det else "—",
    help="% of IaC-eligible resources tracked by Terraform "
         "(orphan-filtered: auto-managed children excluded from denominator).",
)
h2.metric(
    "✅ Compliance",
    f"{_compliance_pct}%" if pol else "—",
    help="% of in-scope resources passing every Rego policy.",
)
h3.metric(
    "🟡 Drift",
    _drift if det else "—",
    delta=("requires attention" if _drift else None),
    delta_color="inverse",
    help="Resources whose cloud values differ from HCL.",
)
h4.metric(
    "📦 Managed",
    _total_managed if det else "—",
    help="Total resources tracked by Terraform (compliant + drifted).",
)

# Empty-state hint: NO engine has a snapshot yet
if all(v is None for v in snapshots.values()):
    st.info(
        "No engine has run yet for this project (or snapshots persistence "
        "is disabled). Visit each engine page and click its Run button to "
        "populate the Dashboard.",
        icon="ℹ️",
    )


# --- 4 Engine Cards ---------------------------------------------------

st.markdown("---")
st.markdown("### Engine Status")
st.caption(
    "Each card shows the most recent run for that engine. "
    "Click **View →** to open the engine page."
)

# 2x2 grid (top row + bottom row).
row1_c1, row1_c2 = st.columns(2)
row2_c1, row2_c2 = st.columns(2)
_card_slots = {
    "importer":   row1_c1,
    "translator": row1_c2,
    "detector":   row2_c1,
    "policy":     row2_c2,
}


def _engine_pie_slices(engine: str, data: dict) -> list:
    """Return pie-chart slice spec for one engine: [(label, value, color)].

    PUI-2v (2026-04-30) -- industry-parity engine cards:
    instead of just showing a one-line headline, each card includes a
    donut chart so the operator's eye lands on the relative proportions
    immediately. Color choices match the rest of the UI:
      green   = success / compliant / passing
      yellow  = needs attention / warning / drift / MED severity
      red     = HIGH severity / failed
      blue    = LOW severity (informational)
      gray    = skipped / no-op
    """
    if engine == "importer":
        return [
            ("Imported",        data.get("imported", 0),         "#22c55e"),
            ("Needs attention", data.get("needs_attention", 0),  "#f59e0b"),
            ("Failed",          data.get("failed", 0),           "#ef4444"),
            ("Skipped",         data.get("skipped", 0),          "#6b7280"),
        ]
    if engine == "translator":
        return [
            ("Translated",      data.get("translated", 0),       "#22c55e"),
            ("Needs attention", data.get("needs_attention", 0),  "#f59e0b"),
            ("Failed",          data.get("failed", 0),           "#ef4444"),
            ("Skipped",         data.get("skipped", 0),          "#6b7280"),
        ]
    if engine == "detector":
        return [
            ("Compliant",  data.get("compliant_count", 0),         "#22c55e"),
            ("Drift",      data.get("drifted_count", 0),           "#f59e0b"),
            ("Unmanaged",  data.get("unmanaged_orphan_count",
                                     data.get("unmanaged_count", 0)),
                                                                    "#ef4444"),
        ]
    if engine == "policy":
        return [
            ("Compliant", data.get("compliant_resources", 0),     "#22c55e"),
            ("HIGH",      data.get("high_count", 0),              "#ef4444"),
            ("MED",       data.get("med_count", 0),               "#f59e0b"),
            ("LOW",       data.get("low_count", 0),               "#3b82f6"),
        ]
    return []


def _render_engine_pie(engine: str, data: dict) -> Optional[alt.Chart]:
    """Build an Altair donut chart for one engine. Returns None if no
    non-zero slices (engine ran but produced 0/0/0/0 -- chart would
    be empty + visually confusing; show "no data" caption instead).
    """
    slices = _engine_pie_slices(engine, data)
    # Drop zero-value slices so the donut shows actual proportions
    # (Altair would render them as 0-degree wedges otherwise).
    nonzero = [(label, val, color) for (label, val, color) in slices if val > 0]
    if not nonzero:
        return None
    df = pd.DataFrame(
        [{"category": lbl, "value": val} for (lbl, val, _) in nonzero]
    )
    color_scale = alt.Scale(
        domain=[lbl for (lbl, _, _) in nonzero],
        range=[color for (_, _, color) in nonzero],
    )
    chart = (
        alt.Chart(df)
        .mark_arc(innerRadius=40, outerRadius=70)
        .encode(
            theta=alt.Theta(field="value", type="quantitative", stack=True),
            color=alt.Color(
                field="category",
                type="nominal",
                scale=color_scale,
                legend=alt.Legend(
                    orient="right",
                    title=None,
                    labelFontSize=11,
                    symbolType="square",
                ),
            ),
            tooltip=[
                alt.Tooltip("category:N", title="Bucket"),
                alt.Tooltip("value:Q", title="Count"),
            ],
        )
        .properties(width=180, height=160)
        .configure_view(strokeOpacity=0)
        .configure(background="transparent")
    )
    return chart


def _render_engine_card(engine: str, container) -> None:
    """Render one engine card into a Streamlit container."""
    env = snapshots.get(engine)
    data = _envelope_data(env)
    written_at = _envelope_written_at(env)

    with container:
        with st.container(border=True):
            st.markdown(f"#### {_ENGINE_LABELS[engine]}")
            if data is None:
                st.caption(
                    f"_No snapshot yet — visit the page and run the "
                    f"engine to populate this card._"
                )
            else:
                st.caption(
                    f"Last run: **{_format_relative_time(written_at)}**"
                    + (f" · {round(data.get('duration_s', 0), 1)}s"
                       if data.get("duration_s") else "")
                )
                # PUI-2v: 2-column layout per card -- donut chart on the
                # left for instant visual parse, headline text on the
                # right for the precise numbers.
                pie_col, text_col = st.columns([1, 1])
                pie = _render_engine_pie(engine, data)
                with pie_col:
                    if pie is not None:
                        st.altair_chart(pie, use_container_width=True)
                    else:
                        st.caption("_(no non-zero slices)_")
                with text_col:
                    st.markdown(f"_{_engine_headline(engine, data)}_")

            # Page link (works only with new st.page_link API; gracefully
            # falls back to a plain markdown link).
            try:
                st.page_link(
                    _ENGINE_PAGE_PATHS[engine],
                    label=f"Open {_ENGINE_LABELS[engine].split(' ', 1)[1]} →",
                    icon="↗️",
                )
            except Exception:  # noqa: BLE001
                # Older Streamlit lacks page_link; render a help line.
                st.caption(
                    f"_Open the **{_ENGINE_LABELS[engine]}** page from "
                    f"the sidebar._"
                )


for eng, slot in _card_slots.items():
    _render_engine_card(eng, slot)


# --- Recent activity feed --------------------------------------------

st.markdown("---")
st.markdown("### Recent Activity")
st.caption(
    "Last snapshots across all engines (newest first). "
    "Click an entry to open the engine page."
)

# Aggregate history from all 4 engines.
all_history: list[tuple[str, dict]] = []  # [(engine, history_entry)]
for engine in _ENGINES:
    entries = _read_history(engine, project_id, limit=10)
    for entry in entries:
        all_history.append((engine, entry))

# Sort by timestamp DESC (timestamp string is ISO-8601 lexicographic).
all_history.sort(key=lambda pair: pair[1].get("timestamp", ""), reverse=True)
all_history = all_history[:10]  # global cap

if not all_history:
    st.info(
        "No activity yet — once you run engines, their snapshots will "
        "appear here.",
        icon="ℹ️",
    )
else:
    for i, (engine, entry) in enumerate(all_history):
        ts = entry.get("timestamp", "")
        # Read the envelope to get headline metric for this specific run.
        env = _read_history_entry(entry.get("gs_uri", ""))
        data = _envelope_data(env) or {}
        with st.container(border=True):
            c1, c2, c3 = st.columns([2, 3, 5])
            c1.markdown(f"**{_format_relative_time(ts)}**")
            c1.caption(ts)
            c2.markdown(_ENGINE_LABELS[engine])
            c3.markdown(_engine_headline(engine, data))


# --- Footer / debug expander -----------------------------------------

with st.expander("Raw snapshot envelopes (debug)", expanded=False):
    for engine in _ENGINES:
        st.markdown(f"**{_ENGINE_LABELS[engine]}** -- "
                    f"latest snapshot envelope:")
        st.json(snapshots.get(engine) or {"_empty": True})
