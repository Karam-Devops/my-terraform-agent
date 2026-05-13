# app/pages/7_🚨_SRE_Agent.py
"""SRE / Incident Response Agent page — Phase 8 Day 1.

Enterprise-grade triage console with a deliberate 3-zone layout:

  ┌──────────────────────────────────────────────────────────────────┐
  │ STICKY CONTEXT BAR  — project · lookback · auto-poll · queue dpth │
  ├──────────────┬───────────────────────────────────────────────────┤
  │              │ HERO STRIP   sev · time · sources · evidence       │
  │ SIDE QUEUE   │                                                    │
  │ (alert cards │ HYPOTHESIS CARDS   ranked w/ confidence gauges     │
  │  by sev)     │                                                    │
  │              │ EVIDENCE TIMELINE                                  │
  │              │                                                    │
  │              │ SOURCE CHIPS   per-collector status + duration     │
  │              │                                                    │
  │              │ ACTIONS   Ack · Defer · Refine · Open in Console   │
  └──────────────┴───────────────────────────────────────────────────┘

Day-1 scope (this commit):
  * Pulls alerts from the configured Pub/Sub subscription (or shows a
    "configure Pub/Sub" empty state + a "Load demo alert" button).
  * Runs ``sre.run.run_incident_triage()`` against the selected alert.
    Day-1 evidence/hypotheses are empty stubs; the UI still renders
    correctly because the result shape is final.
  * Ack / Nack buttons wired but conservative (ack only after operator
    confirms with the explicit button — never auto-ack on triage).

Day-2 fills in evidence + hypothesis content — no UI work needed,
the same widgets just become populated.

Severity color tokens (consistent with the Day-0 mockup):
  SEV1 #ef4444 · SEV2 #f97316 · SEV3 #f59e0b · SEV4 #3b82f6 · INFO #6b7280
"""

from __future__ import annotations

import os
import time
from typing import Dict, List, Optional

import streamlit as st

from app.ui.auth import auth_status_banner, resolve_tenant_id
from app.ui.error_surface import render_error
from app.ui.sidebar import render_sidebar
from app.ui.theme import apply_theme_polish

from common.errors import PreflightError

from sre.results import (
    AlertEnvelope,
    Hypothesis,
    IncidentResult,
    SourceTiming,
    SEV1, SEV2, SEV3, SEV4, SEV_INFO,
)
from sre.run import run_incident_triage
from sre.triggers import gcp_pubsub
from sre.triggers.gcp_pubsub import PubSubUnavailable


# ---------------------------------------------------------------------------
# Severity + confidence color tokens.
#
# Inline-styled spans (instead of status_pill) because we want full
# control over background/border alpha per severity — the platform's
# generic info/warning/error pills aren't expressive enough for a
# 5-level severity scale.
# ---------------------------------------------------------------------------

_SEV_COLORS: Dict[str, str] = {
    SEV1:     "#ef4444",   # red
    SEV2:     "#f97316",   # orange
    SEV3:     "#f59e0b",   # amber
    SEV4:     "#3b82f6",   # blue
    SEV_INFO: "#6b7280",   # gray
}

_CONF_COLORS: Dict[str, str] = {
    "HIGH":   "#00C853",
    "MEDIUM": "#FFA726",
    "LOW":    "#EF5350",
}

_SOURCE_STATUS_COLORS = {
    "ok":      "#00C853",
    "partial": "#FFA726",
    "failed":  "#EF5350",
}


# ---------------------------------------------------------------------------
# Page chrome
# ---------------------------------------------------------------------------

st.set_page_config(
    page_title="mtagent · SRE Agent",
    page_icon="🚨",
    layout="wide",
)
apply_theme_polish()

project_id = render_sidebar()
tenant_id = resolve_tenant_id()

# Title row with a single-line status caption. The auth banner goes in
# a small caption so multi-tenant deployments make the operator's
# identity visible without dominating the page.
st.title("🚨 SRE / Incident Response Agent")
st.caption(
    "Triage incoming alerts in seconds. The agent pulls evidence from "
    "Cloud Asset, IAM audit logs, and Cloud Build, correlates it to the "
    "alert window, and ranks probable causes."
)
st.caption(auth_status_banner())

if not project_id:
    st.warning("Pick a project in the sidebar to get started.", icon="⚠️")
    st.stop()


# ---------------------------------------------------------------------------
# Session state initialisation.
#
# Streamlit re-runs the whole page on every interaction, so all "stickiness"
# (the alert queue snapshot, the most-recent triage result, auto-poll toggle)
# lives in session_state. Keys are namespaced with `sre_` so they don't
# collide with the other engine pages.
# ---------------------------------------------------------------------------

st.session_state.setdefault("sre_alerts", [])           # type: List[AlertEnvelope]
st.session_state.setdefault("sre_selected_alert_id", None)
st.session_state.setdefault("sre_triage_result", None)  # IncidentResult | None
st.session_state.setdefault("sre_last_pull_at", 0.0)
st.session_state.setdefault("sre_auto_poll", False)
st.session_state.setdefault("sre_lookback_min", 60)
st.session_state.setdefault("sre_subscription_id", gcp_pubsub.DEFAULT_SUBSCRIPTION_ID)
st.session_state.setdefault("sre_status_message", "")


# ---------------------------------------------------------------------------
# Sticky context bar.
#
# Single row at top of page with 5 cells: project pin, lookback selector,
# subscription, auto-poll toggle, queue depth. Implemented as a 5-column
# st.columns row inside a container so the visual band stays consistent
# even when the rest of the page is empty.
# ---------------------------------------------------------------------------

with st.container():
    c1, c2, c3, c4, c5 = st.columns([2, 1.2, 2, 1.2, 1])
    with c1:
        st.markdown(f"**Project**\n`{project_id}`")
    with c2:
        st.session_state["sre_lookback_min"] = st.selectbox(
            "Lookback window",
            options=[15, 30, 60, 120, 240],
            index=[15, 30, 60, 120, 240].index(
                st.session_state["sre_lookback_min"]
            ),
            help="How far back the agent scans for evidence. The 'last hour' "
                 "default covers the most common 'what changed?' triage flow.",
        )
    with c3:
        st.session_state["sre_subscription_id"] = st.text_input(
            "Pub/Sub subscription",
            value=st.session_state["sre_subscription_id"],
            help="Pull subscription receiving Cloud Monitoring alerts. "
                 "Created idempotently by scripts/sre_setup_gcp.sh.",
        )
    with c4:
        st.session_state["sre_auto_poll"] = st.toggle(
            "Auto-poll",
            value=st.session_state["sre_auto_poll"],
            help="Re-pull the subscription every 10s automatically. "
                 "Off by default so the demo stays predictable.",
        )
    with c5:
        st.metric("Queue", len(st.session_state["sre_alerts"]))


# ---------------------------------------------------------------------------
# Alert pulling
# ---------------------------------------------------------------------------

def _pull_alerts(*, subscription_id: str, max_messages: int = 10) -> int:
    """Pull alerts from Pub/Sub into session_state. Returns count added."""
    try:
        envelopes = gcp_pubsub.list_pending_alerts(
            project_id=project_id,
            subscription_id=subscription_id,
            max_messages=max_messages,
        )
    except PubSubUnavailable as e:
        # Surface as a soft banner; don't blow up the page. The empty
        # state below will render the "Load demo alert" path so the
        # operator can still see the engine working.
        st.session_state["sre_status_message"] = e.user_hint
        return 0
    except PreflightError as e:
        st.session_state["sre_status_message"] = e.user_hint
        return 0

    # De-dup against what's already queued (Pub/Sub may redeliver if
    # the previous pull's ack didn't land in time). Keyed on alert_id.
    existing_ids = {a.alert_id for a in st.session_state["sre_alerts"]}
    added = 0
    for env in envelopes:
        if env.alert_id not in existing_ids:
            st.session_state["sre_alerts"].append(env)
            existing_ids.add(env.alert_id)
            added += 1
    st.session_state["sre_last_pull_at"] = time.time()
    st.session_state["sre_status_message"] = (
        f"Pulled {added} new alert(s)" if added else "No new alerts"
    )
    return added


# ---------------------------------------------------------------------------
# Triage-area render helpers. Defined here (above the page-render block
# that calls them) so Streamlit's top-to-bottom re-execution model resolves
# the names correctly on every rerun. Kept inline with the page rather
# than split into app/ui/ because every helper is tightly coupled to
# session_state keys + closure references like project_id.
# ---------------------------------------------------------------------------


def _render_triage(
    alert: AlertEnvelope, *, project_id: str, tenant_id: str,
) -> None:
    """Render the right-column triage panel for the chosen alert."""
    color = _SEV_COLORS.get(alert.severity, "#6b7280")

    # Alert header card with severity-tinted left border, summary, and
    # a quick-access link to the GCP Console (if the alert carried one).
    console_url = alert.labels.get("console_url", "")
    st.markdown(
        f"""
        <div style="
            border-left: 6px solid {color};
            background-color: #1A1F2C;
            border-radius: 8px;
            padding: 14px 18px;
            margin-bottom: 12px;
        ">
            <div style="font-size: 0.75em; color: {color}; font-weight: 700; letter-spacing: 0.5px;">
                {alert.severity} · {alert.source} · fired at {alert.fired_at}
            </div>
            <div style="font-size: 1.2em; font-weight: 700; color: #E5E9F2; margin-top: 4px;">
                {alert.policy_name}
            </div>
            <div style="color: #9aa0aa; margin-top: 6px;">
                {alert.summary or '(no summary)'}
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    # Run-triage / Re-run controls. We treat the triage call as cheap
    # enough to run on click — Day 2/3 source pulls will take longer
    # so we'll add a spinner + status updates then.
    rc1, rc2, _ = st.columns([1, 1, 3])
    with rc1:
        run_clicked = st.button(
            "▶ Run triage" if not st.session_state["sre_triage_result"]
            else "↻ Re-run",
            type="primary",
            use_container_width=True,
        )
    with rc2:
        if console_url:
            st.link_button(
                "🔗 Console", url=console_url, use_container_width=True,
            )

    if run_clicked:
        with st.spinner("Collecting evidence + ranking hypotheses…"):
            try:
                result = run_incident_triage(
                    alert,
                    project_id=project_id,
                    lookback_min=st.session_state["sre_lookback_min"],
                    tenant_id=tenant_id,
                )
            except PreflightError as e:
                render_error(e, context="running incident triage")
                return
            except Exception as e:  # noqa: BLE001
                render_error(e, context="running incident triage")
                return
        st.session_state["sre_triage_result"] = result

    result: Optional[IncidentResult] = st.session_state["sre_triage_result"]
    if result is None:
        st.caption("Click **Run triage** to start the engine.")
        return

    # ---- Hero metrics strip -------------------------------------------
    h1, h2, h3, h4 = st.columns(4)
    with h1:
        st.metric("Severity", alert.severity)
    with h2:
        st.metric("Duration", f"{result.duration_s:.1f}s")
    with h3:
        st.metric("Evidence", result.evidence_count)
    with h4:
        top = result.top_hypothesis
        st.metric(
            "Top hypothesis",
            top.confidence if top else "—",
            delta=f"{top.confidence_pct}%" if top else None,
        )

    # ---- Hypothesis cards ----------------------------------------------
    st.markdown("### Probable causes")
    if not result.hypotheses:
        # Day-1 stub path: explain why nothing's here yet. Removes the
        # "did it break?" anxiety during the early-day demo.
        st.info(
            "Hypotheses will appear here once the correlator and ranker "
            "land (Day 2/3). The engine is wired end-to-end already — "
            "evidence sources just need to be plugged in.",
            icon="🔬",
        )
    else:
        for hyp in result.hypotheses:
            _render_hypothesis_card(hyp)

    # ---- Evidence timeline ---------------------------------------------
    with st.expander(
        f"📜 Evidence timeline ({result.evidence_count} items)",
        expanded=False,
    ):
        if not result.evidence:
            st.caption("No evidence collected yet.")
        else:
            # Sort newest-first, render as a compact list. Day 2 will
            # add filtering by hypothesis (click a hypothesis card →
            # this list narrows to its cited_evidence).
            for ev in sorted(
                result.evidence, key=lambda e: e.timestamp, reverse=True,
            ):
                st.markdown(
                    f"**{ev.timestamp}** · `{ev.source}` · "
                    f"{ev.change_type} on `{ev.resource_ref}` · "
                    f"by **{ev.actor or 'system'}** — {ev.summary}"
                )

    # ---- Source chips --------------------------------------------------
    st.markdown("### Sources scanned")
    _render_source_chips(result.source_timings)

    # ---- Notes / errors / stub markers --------------------------------
    if result.notes:
        with st.expander(f"📝 Notes ({len(result.notes)})", expanded=False):
            for note in result.notes:
                st.write(f"• {note}")
    if result.errors:
        with st.expander(
            f"⚠️ Errors ({len(result.errors)})", expanded=True,
        ):
            for err in result.errors:
                st.error(err)

    # ---- Action bar ----------------------------------------------------
    st.divider()
    st.markdown("### Actions")
    a1, a2, a3 = st.columns(3)
    with a1:
        if st.button(
            "✅ Acknowledge",
            use_container_width=True,
            help="Acks the Pub/Sub message and removes the alert from "
                 "the queue. Use after you're confident in the triage.",
        ):
            _do_ack(alert)
    with a2:
        if st.button(
            "⏸ Defer (nack)",
            use_container_width=True,
            help="Nacks the message so Pub/Sub redelivers it later. "
                 "Use when you can't triage right now.",
        ):
            _do_nack(alert)
    with a3:
        st.button(
            "🤖 Refine with Claude",
            use_container_width=True,
            disabled=True,
            help="Coming Day 3 — re-ranks hypotheses with extra context "
                 "(operator notes, related incidents).",
        )


def _render_hypothesis_card(hyp: Hypothesis) -> None:
    """Render one hypothesis as a confidence-bar card with reasoning."""
    color = _CONF_COLORS.get(hyp.confidence, "#6b7280")
    # Filled-progress bar via inline HTML — Streamlit's st.progress only
    # accepts 0..1 but we want a labelled, color-coded bar with the
    # confidence band visible. The HTML version is dependency-free and
    # renders identically on all browsers.
    bar_html = f"""
    <div style="background:#0E1117; border-radius:6px; height:8px; overflow:hidden; margin-top:6px;">
        <div style="background:{color}; width:{hyp.confidence_pct}%; height:100%;"></div>
    </div>
    """
    st.markdown(
        f"""
        <div style="
            background:#1A1F2C;
            border:1px solid #2A3142;
            border-radius:8px;
            padding:14px 16px;
            margin-bottom:10px;
        ">
            <div style="display:flex; justify-content:space-between; align-items:baseline;">
                <div style="font-weight:700; font-size:1.05em; color:#E5E9F2;">
                    #{hyp.rank} · {hyp.headline}
                </div>
                <div style="font-size:0.85em; color:{color}; font-weight:700;">
                    {hyp.confidence} · {hyp.confidence_pct}%
                </div>
            </div>
            {bar_html}
        </div>
        """,
        unsafe_allow_html=True,
    )
    if hyp.reasoning:
        with st.expander("Why this?", expanded=False):
            for bullet in hyp.reasoning:
                st.write(f"• {bullet}")
            if hyp.cited_evidence:
                st.caption(
                    f"Cites evidence: {', '.join(hyp.cited_evidence)}"
                )
    # Action buttons (revert PR, post to Slack, etc.) — Day 3 wires
    # the recommended_actions list into real buttons. Today we render
    # them as inert chips so the layout is right.
    if hyp.recommended_actions:
        with st.expander("Recommended actions", expanded=False):
            for action in hyp.recommended_actions:
                st.button(
                    action.get("label", "Action"),
                    key=f"hyp{hyp.rank}_act_{action.get('label','x')}",
                    disabled=True,
                    help=f"kind={action.get('kind')}",
                )


def _render_source_chips(timings: List[SourceTiming]) -> None:
    """Horizontal row of source-status chips with item count + duration."""
    if not timings:
        st.caption("No sources scanned.")
        return
    cols = st.columns(len(timings))
    for i, t in enumerate(timings):
        color = _SOURCE_STATUS_COLORS.get(t.status, "#6b7280")
        with cols[i]:
            st.markdown(
                f"""
                <div style="
                    background:#1A1F2C;
                    border:1px solid {color}80;
                    border-left:4px solid {color};
                    border-radius:6px;
                    padding:10px 12px;
                ">
                    <div style="font-size:0.75em; color:{color}; font-weight:700; letter-spacing:0.5px;">
                        {t.status.upper()}
                    </div>
                    <div style="font-weight:600; color:#E5E9F2; margin-top:2px;">
                        {t.source}
                    </div>
                    <div style="font-size:0.8em; color:#9aa0aa; margin-top:2px;">
                        {t.item_count} items · {t.duration_ms} ms
                    </div>
                    {f'<div style="font-size:0.75em; color:#9aa0aa; margin-top:4px;">{t.error}</div>' if t.error else ''}
                </div>
                """,
                unsafe_allow_html=True,
            )


def _do_ack(alert: AlertEnvelope) -> None:
    """Best-effort ack + queue cleanup. Silent on success."""
    if alert.pubsub_ack_id:
        try:
            gcp_pubsub.ack(
                project_id=project_id,
                ack_ids=[alert.pubsub_ack_id],
                subscription_id=st.session_state["sre_subscription_id"],
            )
        except Exception as e:  # noqa: BLE001
            st.warning(f"Ack failed (queue cleanup still applied): {e}")
    # Always remove from local queue + clear selection.
    st.session_state["sre_alerts"] = [
        a for a in st.session_state["sre_alerts"]
        if a.alert_id != alert.alert_id
    ]
    st.session_state["sre_selected_alert_id"] = None
    st.session_state["sre_triage_result"] = None
    st.session_state["sre_status_message"] = (
        f"Acknowledged {alert.alert_id}"
    )


def _do_nack(alert: AlertEnvelope) -> None:
    """Best-effort nack so Pub/Sub redelivers; keeps local queue clean."""
    if alert.pubsub_ack_id:
        try:
            gcp_pubsub.nack(
                project_id=project_id,
                ack_ids=[alert.pubsub_ack_id],
                subscription_id=st.session_state["sre_subscription_id"],
            )
        except Exception as e:  # noqa: BLE001
            st.warning(f"Nack failed: {e}")
    st.session_state["sre_alerts"] = [
        a for a in st.session_state["sre_alerts"]
        if a.alert_id != alert.alert_id
    ]
    st.session_state["sre_selected_alert_id"] = None
    st.session_state["sre_triage_result"] = None
    st.session_state["sre_status_message"] = f"Deferred {alert.alert_id}"


# Action row beneath the context bar: manual pull + clear queue +
# load-demo (always available so the page can be demoed without
# Pub/Sub set up yet).
ac1, ac2, ac3, ac4 = st.columns([1, 1, 1, 3])
with ac1:
    if st.button("🔄 Pull now", type="primary", use_container_width=True):
        _pull_alerts(subscription_id=st.session_state["sre_subscription_id"])
with ac2:
    if st.button("🧪 Load demo alert", use_container_width=True,
                 help="Drops a hand-crafted SEV2 alert into the queue. "
                      "Useful when Pub/Sub isn't configured yet."):
        demo = AlertEnvelope(
            alert_id=f"demo-{int(time.time())}",
            source="demo_seeder",
            fired_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            policy_name="ALB 5xx > 5% (DEMO)",
            summary="5xx error rate on payments-prod-alb has exceeded the "
                    "warning threshold (5%). p95 latency stable.",
            severity=SEV2,
            resource_refs=[
                f"projects/{project_id}/instances/payments-prod-alb",
            ],
            project_id=project_id,
            labels={"team": "payments", "env": "prod"},
        )
        st.session_state["sre_alerts"].append(demo)
        st.session_state["sre_status_message"] = "Loaded demo alert"
with ac3:
    if st.button("🗑️ Clear queue", use_container_width=True):
        st.session_state["sre_alerts"] = []
        st.session_state["sre_selected_alert_id"] = None
        st.session_state["sre_triage_result"] = None
        st.session_state["sre_status_message"] = "Queue cleared"

if st.session_state["sre_status_message"]:
    st.caption(st.session_state["sre_status_message"])

st.divider()


# ---------------------------------------------------------------------------
# Main two-column layout: side queue + triage area.
#
# 1:3 ratio so the main triage area dominates. Queue is dense (short
# alert cards), triage area is wide (hypothesis cards + timeline + source
# chips render comfortably).
# ---------------------------------------------------------------------------

queue_col, main_col = st.columns([1, 3], gap="large")


# --- Queue (left column) ---------------------------------------------------

with queue_col:
    st.markdown("### Queue")

    alerts: List[AlertEnvelope] = st.session_state["sre_alerts"]
    if not alerts:
        st.info(
            "No alerts yet. Click **Pull now** to fetch from Pub/Sub, "
            "or **Load demo alert** to see the agent in action.",
            icon="📭",
        )
    else:
        # Sort: most recent first (matches operator scan habit).
        sorted_alerts = sorted(
            alerts, key=lambda a: a.fired_at, reverse=True,
        )
        for env in sorted_alerts:
            color = _SEV_COLORS.get(env.severity, "#6b7280")
            is_selected = (
                env.alert_id == st.session_state["sre_selected_alert_id"]
            )
            # Each alert card is a button so the operator can pick it
            # with a single click. Severity is rendered as a leading
            # color block via HTML — Streamlit buttons don't accept
            # rich markup, so the card itself sits ABOVE the button
            # and the button is the click target with a minimal label.
            border = (
                f"2px solid {color}" if is_selected
                else f"1px solid {color}55"
            )
            st.markdown(
                f"""
                <div style="
                    border-left: 4px solid {color};
                    background-color: #1A1F2C;
                    border-top: {border};
                    border-right: {border};
                    border-bottom: {border};
                    border-radius: 6px;
                    padding: 8px 12px;
                    margin-bottom: 6px;
                ">
                    <div style="font-size: 0.75em; color: {color}; font-weight: 700; letter-spacing: 0.5px;">
                        {env.severity} · {env.source}
                    </div>
                    <div style="font-weight: 600; font-size: 0.95em; color: #E5E9F2; margin-top: 2px;">
                        {env.policy_name}
                    </div>
                    <div style="font-size: 0.8em; color: #9aa0aa; margin-top: 4px;">
                        {env.fired_at}
                    </div>
                </div>
                """,
                unsafe_allow_html=True,
            )
            if st.button(
                "Triage →" if not is_selected else "● Selected",
                key=f"sre_pick_{env.alert_id}",
                use_container_width=True,
                disabled=is_selected,
            ):
                st.session_state["sre_selected_alert_id"] = env.alert_id
                st.session_state["sre_triage_result"] = None  # clear stale


# --- Triage area (right column) -------------------------------------------

with main_col:
    selected_id = st.session_state["sre_selected_alert_id"]
    if not selected_id:
        # Empty-state hero — keeps the page from looking broken on first
        # load. Three help cards explain what happens after pulling alerts.
        st.markdown("### Pick an alert to triage")
        st.write(
            "Select an alert from the queue on the left. The agent will:"
        )
        hc1, hc2, hc3 = st.columns(3)
        with hc1:
            st.markdown("**1. Collect**\n\nPulls asset changes, IAM events, "
                        "and deploys from the lookback window.")
        with hc2:
            st.markdown("**2. Correlate**\n\nScores each change for "
                        "temporal + resource overlap with the alert.")
        with hc3:
            st.markdown("**3. Rank**\n\nClaude writes plain-English "
                        "hypotheses citing the specific evidence behind each.")
    else:
        # Find the selected alert envelope.
        selected = next(
            (a for a in st.session_state["sre_alerts"] if a.alert_id == selected_id),
            None,
        )
        if selected is None:
            st.warning("Selected alert is no longer in the queue.")
        else:
            _render_triage(selected, project_id=project_id, tenant_id=tenant_id)


# ---------------------------------------------------------------------------
# Auto-poll. Streamlit doesn't have a true timer; the cleanest pattern
# is st.rerun() after a sleep, gated by the toggle so we don't busy-loop.
# 10 s cadence balances "feels live" against Pub/Sub QPS.
# ---------------------------------------------------------------------------

if st.session_state["sre_auto_poll"]:
    elapsed = time.time() - st.session_state["sre_last_pull_at"]
    if elapsed >= 10.0:
        _pull_alerts(subscription_id=st.session_state["sre_subscription_id"])
    # Schedule the next rerun. time.sleep blocks the script; that's fine
    # in Streamlit's threading model (each session is its own thread).
    time.sleep(2.0)
    st.rerun()
