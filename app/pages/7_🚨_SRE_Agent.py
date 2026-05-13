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

# Day-3: persistence + refine. Both are imported lazily inside the page
# render so a missing dependency (e.g., google-cloud-storage for the
# gs:// backend in local-dev) doesn't break the page import. The
# top-level imports here are pure-Python and always work.
from sre.output import result_persistence as _persist
from sre.llm.refine import refine_with_notes


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
st.session_state.setdefault("sre_restore_offered", False)
st.session_state.setdefault("sre_refine_notes", "")


# ---------------------------------------------------------------------------
# Alert pulling — defined HERE (above the Restore banner) because the
# Restore + Past Triages handlers need to call _pull_alerts to top-up
# the queue with any other unacked alerts that vanished from
# session_state on a hard browser refresh. Streamlit re-executes the
# script top-to-bottom, so a function call must appear AFTER its def
# in script order.
# ---------------------------------------------------------------------------

def _pull_alerts(*, subscription_id: str, max_messages: int = 10) -> int:
    """Pull alerts from Pub/Sub into session_state. Returns count added.

    Pub/Sub's ``pull`` returns whatever's *immediately leasable* —
    often a fraction of what's actually in the subscription. To make
    the "Pull now" button feel like "drain everything", we loop
    client-side until a pull returns zero new messages or we hit a
    safety cap. This is the standard pattern for batch-style drain
    semantics on top of Pub/Sub's per-call best-effort delivery.
    """
    # Safety cap: prevents a misbehaving subscription (e.g., constant
    # redelivery) from looping forever. 10 iterations × 10 messages =
    # up to 100 alerts per click, comfortably above realistic burst.
    _MAX_PULL_ITERATIONS = 10

    existing_ids = {a.alert_id for a in st.session_state["sre_alerts"]}
    added = 0

    for _ in range(_MAX_PULL_ITERATIONS):
        try:
            envelopes = gcp_pubsub.list_pending_alerts(
                project_id=project_id,
                subscription_id=subscription_id,
                max_messages=max_messages,
            )
        except PubSubUnavailable as e:
            st.session_state["sre_status_message"] = e.user_hint
            return added
        except PreflightError as e:
            st.session_state["sre_status_message"] = e.user_hint
            return added

        if not envelopes:
            # Empty pull = subscription is drained (or truly quiet). Stop.
            break

        added_this_round = 0
        for env in envelopes:
            if env.alert_id not in existing_ids:
                st.session_state["sre_alerts"].append(env)
                existing_ids.add(env.alert_id)
                added += 1
                added_this_round += 1

        # If we got messages but they were all duplicates of what's
        # already in the queue (Pub/Sub redelivery from a previous
        # unacked lease), stop — looping would just keep getting the
        # same duplicates.
        if added_this_round == 0:
            break

    st.session_state["sre_last_pull_at"] = time.time()
    st.session_state["sre_status_message"] = (
        f"Pulled {added} new alert(s)" if added else "No new alerts"
    )
    return added


# ---------------------------------------------------------------------------
# Restore-from-persistence banner (Day 3).
#
# Streamlit's session_state evaporates on browser refresh / tab close
# / Cloud Run replica swap. The persistence module writes the last
# triage to gs:// (prod) or ~/.mtagent-sre/snapshots/ (local) keyed by
# <tenant>::<project>. On page load, peek the registry — if there's a
# recent saved triage and the operator hasn't already declined to
# restore it, show a single non-blocking banner above the queue.
#
# user_key matches the orchestrator's snapshot save (see sre/run.py's
# _persist_best_effort). Same tenant_id ⊕ project_id slot.
# ---------------------------------------------------------------------------

_user_key = f"{tenant_id}::{project_id}" if tenant_id != "default" else f"default::{project_id}"
if not st.session_state["sre_restore_offered"] and st.session_state["sre_triage_result"] is None:
    try:
        _info = _persist.get_last_triage_info(user_key=_user_key)
    except Exception:  # noqa: BLE001
        _info = None
    if _info:
        rb_cols = st.columns([5, 1, 1])
        with rb_cols[0]:
            saved_min_ago = max(0, int((time.time() - _info["saved_at"]) / 60))
            st.info(
                f"Found a saved triage for alert **{_info['alert_id']}** "
                f"from {saved_min_ago} min ago. Restore?",
                icon="🔁",
            )
        with rb_cols[1]:
            if st.button("Restore", type="primary", use_container_width=True,
                         key="sre_restore_yes"):
                restored = _persist.load_result(user_key=_user_key)
                if restored is not None:
                    # Re-hydrate session_state: re-inject the alert into the
                    # queue (so the queue card is clickable), select it, and
                    # park the loaded result so Run-triage isn't required.
                    in_queue = any(
                        a.alert_id == restored.alert.alert_id
                        for a in st.session_state["sre_alerts"]
                    )
                    if not in_queue:
                        st.session_state["sre_alerts"].append(restored.alert)
                    st.session_state["sre_selected_alert_id"] = restored.alert.alert_id
                    st.session_state["sre_triage_result"] = restored
                    st.session_state["sre_restore_offered"] = True

                    # Day-4 fix: a hard browser refresh wipes
                    # session_state, so after Restore the queue contains
                    # ONLY the snapshotted alert — any other unacked
                    # alerts the operator had in the queue at save-time
                    # are gone. Auto-pull the subscription so they
                    # reappear. _pull_alerts' alert_id dedup prevents
                    # double-adding the restored one if Pub/Sub also
                    # still has it un-acked.
                    pulled_back = _pull_alerts(
                        subscription_id=st.session_state["sre_subscription_id"],
                    )
                    if pulled_back:
                        st.session_state["sre_status_message"] = (
                            f"Restored {restored.alert.alert_id} + "
                            f"re-pulled {pulled_back} pending alert(s)"
                        )
                    else:
                        st.session_state["sre_status_message"] = (
                            f"Restored triage for {restored.alert.alert_id}"
                        )
                    st.rerun()
                else:
                    st.warning("Restore failed; the snapshot may be corrupt.")
                    st.session_state["sre_restore_offered"] = True
        with rb_cols[2]:
            if st.button("Dismiss", use_container_width=True,
                         key="sre_restore_no"):
                st.session_state["sre_restore_offered"] = True
                st.rerun()


# ---------------------------------------------------------------------------
# Past Triages selector (Day-4 polish).
#
# Registry keeps a ring buffer of the last 10 triages per user_key.
# Surfacing them lets an operator review what they did earlier in
# the shift, or pull up a prior triage of the same alert for
# pattern-matching. Hidden behind an expander so the page doesn't
# get cluttered for first-time operators.
#
# Only shown when there's actually more than one past triage —
# during the very first session of a day, the registry has just
# one entry which is already the "latest" the Restore banner above
# offers. Showing a one-item dropdown would be noisy.
# ---------------------------------------------------------------------------

try:
    _recent_triages = _persist.list_recent_triages(user_key=_user_key)
except Exception:  # noqa: BLE001
    _recent_triages = []
if len(_recent_triages) >= 2:
    with st.expander(f"📚 Past triages ({len(_recent_triages)} recent)", expanded=False):
        st.caption(
            "Pick any past triage to re-hydrate the page with it. The "
            "underlying snapshot is loaded from gs:// or your local "
            "registry — no engine re-run, no LLM call."
        )
        # Build option labels: "alert_id  ·  XX min ago"
        option_labels = []
        for entry in _recent_triages:
            saved_ago = max(0, int((time.time() - entry.get("saved_at", 0)) / 60))
            option_labels.append(
                f"{entry.get('alert_id', '?')}  ·  {saved_ago} min ago"
            )
        choice_idx = st.selectbox(
            "Past triage",
            options=list(range(len(option_labels))),
            format_func=lambda i: option_labels[i],
            key="sre_past_triage_choice",
        )
        if st.button("Load selected", use_container_width=True,
                     key="sre_load_past_triage"):
            target = _recent_triages[choice_idx]
            try:
                restored_past = _persist.load_result(
                    destination=target["destination"],
                    alert_id=target["alert_id"],
                    user_key=_user_key,
                )
            except Exception as e:  # noqa: BLE001
                restored_past = None
                st.warning(f"Load failed: {e}")
            if restored_past is not None:
                in_queue = any(
                    a.alert_id == restored_past.alert.alert_id
                    for a in st.session_state["sre_alerts"]
                )
                if not in_queue:
                    st.session_state["sre_alerts"].append(restored_past.alert)
                st.session_state["sre_selected_alert_id"] = restored_past.alert.alert_id
                st.session_state["sre_triage_result"] = restored_past
                # Clear any stale refine deltas from a different
                # triage session.
                st.session_state["sre_pre_refine_map"] = None
                st.session_state["sre_refine_notes"] = ""
                # Same Day-4 fix as the Restore banner: also pull
                # from Pub/Sub so other unacked alerts come back
                # into the queue. Without this, loading a past
                # triage leaves the queue with only the loaded
                # alert; the operator would have to click Pull now
                # again separately.
                extra = _pull_alerts(
                    subscription_id=st.session_state["sre_subscription_id"],
                )
                st.session_state["sre_status_message"] = (
                    f"Loaded past triage for {restored_past.alert.alert_id}"
                    + (f" + re-pulled {extra} pending alert(s)" if extra else "")
                )
                st.rerun()
            elif restored_past is None:
                st.warning(
                    "Snapshot couldn't be loaded — it may have been "
                    "pruned. The registry will self-heal on the next save."
                )


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
    # Single-line HTML for the same markdown-parser reason — see
    # _render_hypothesis_card.
    console_url = alert.labels.get("console_url", "")
    st.markdown(
        f'<div style="border-left:6px solid {color};background-color:#1A1F2C;'
        f'border-radius:8px;padding:12px 16px;margin-bottom:10px;">'
        f'<div style="font-size:0.75em;color:{color};font-weight:700;'
        f'letter-spacing:0.5px;">'
        f'{alert.severity} · {alert.source} · fired at {alert.fired_at}</div>'
        f'<div style="font-size:1.15em;font-weight:700;color:#E5E9F2;'
        f'margin-top:4px;">{alert.policy_name}</div>'
        f'<div style="color:#9aa0aa;margin-top:6px;line-height:1.4;">'
        f'{alert.summary or "(no summary)"}</div></div>',
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
        # Fresh triage: any prior refine deltas don't apply to the new
        # ranking. Clear the snapshot map + the operator notes text so
        # the next refine cycle starts clean.
        st.session_state["sre_pre_refine_map"] = None
        st.session_state["sre_refine_notes"] = ""

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
        # Empty-hypotheses branch is reached when correlator.rank() short-
        # circuits on empty evidence. Differentiate the real reasons so
        # the operator knows whether to act (widen lookback) or wait (a
        # collector failed and needs retry).
        any_source_failed = any(
            t.status == "failed" for t in result.source_timings
        )
        if result.evidence_count == 0 and any_source_failed:
            st.warning(
                "No evidence collected because one or more sources failed. "
                "Check the **Sources scanned** chips below for details, then "
                "fix the underlying issue and click **↻ Re-run**.",
                icon="⚠️",
            )
        elif result.evidence_count == 0:
            st.info(
                f"No changes found in `{result.project_id}` during the last "
                f"{result.lookback_min} min. Widen the **Lookback window** "
                "in the top bar, or generate test activity:\n\n"
                "```\ngcloud compute networks update default \\\n"
                f"    --update-labels=sre-demo=$(date +%s) \\\n"
                f"    --project={result.project_id}\n```",
                icon="🔍",
            )
        else:
            # Evidence exists but ranker produced nothing — unusual; means
            # every cluster scored below the correlator's minimum signal
            # threshold. The full timeline below shows what was found so
            # the operator can investigate manually.
            st.info(
                f"Collected {result.evidence_count} evidence items, but the "
                "correlator didn't produce a ranked hypothesis. The full "
                "timeline is below — review it directly.",
                icon="🤷",
            )
    else:
        for hyp in result.hypotheses:
            _render_hypothesis_card(hyp)

    # ---- Evidence timeline ---------------------------------------------
    # Expanded by default — operators want to see the raw "what changed"
    # immediately alongside the ranked hypotheses. The collapsed
    # variant from Day-1 hid the most valuable Day-2/3 output below
    # the fold.
    with st.expander(
        f"📜 Evidence timeline ({result.evidence_count} items)",
        expanded=True,
    ):
        if not result.evidence:
            st.caption("No evidence collected yet.")
        else:
            # Sort newest-first, render as a compact list. Day 2 will
            # add filtering by hypothesis (click a hypothesis card →
            # this list narrows to its cited_evidence).
            # Sort by relevance_score (correlator-assigned) DESC so the
            # most-implicated changes float to the top. Ties broken by
            # timestamp (newer first). Score badge gives the operator
            # a fast visual scan of "which evidence actually matters".
            for ev in sorted(
                result.evidence,
                key=lambda e: (e.relevance_score, e.timestamp),
                reverse=True,
            ):
                # Color the score badge by tier (matches the confidence
                # bar palette used on the hypothesis cards above).
                if ev.relevance_score >= 0.70:
                    score_color = _CONF_COLORS["HIGH"]
                elif ev.relevance_score >= 0.40:
                    score_color = _CONF_COLORS["MEDIUM"]
                else:
                    score_color = _CONF_COLORS["LOW"]
                score_badge = (
                    f'<span style="background:{score_color}26; '
                    f'color:{score_color}; padding:1px 6px; '
                    f'border-radius:8px; font-size:0.8em; '
                    f'font-weight:600; margin-right:6px;">'
                    f'{ev.relevance_score:.2f}</span>'
                )
                st.markdown(
                    f"{score_badge} **{ev.timestamp}** · `{ev.source}` · "
                    f"{ev.change_type} on `{ev.resource_ref}` · "
                    f"by **{ev.actor or 'system'}** — {ev.summary}",
                    unsafe_allow_html=True,
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

    # ---- Refine with operator notes (Day 3) ----------------------------
    #
    # The operator types fresh context ("I rolled back the deploy, alert
    # still firing" / "scheduled maintenance, ignore the SG change") and
    # the LLM re-ranks + rewrites hypotheses. Original ranks stay in
    # session_state until the operator confirms the refined result —
    # they can always Re-run to start over.
    st.divider()
    with st.expander("🤖 Refine with operator notes", expanded=False):
        st.write(
            "Type any context the agent couldn't see — rollbacks, "
            "known maintenance, customer reports — and the agent will "
            "re-rank the hypotheses against your notes."
        )
        notes_val = st.text_area(
            "Operator notes",
            value=st.session_state.get("sre_refine_notes", ""),
            key="sre_refine_notes_input",
            height=100,
            placeholder="e.g., I rolled back the deploy at 10:02 UTC and "
                        "the alert is still firing — please de-emphasize "
                        "the deploy hypothesis.",
        )
        if st.button(
            "Re-rank with these notes",
            type="primary",
            disabled=not notes_val.strip(),
            help="Calls the LLM once. Falls back to the original "
                 "ranking on any failure — your view doesn't lose state.",
        ):
            with st.spinner("Re-ranking hypotheses with your notes…"):
                try:
                    # Capture pre-refine confidence map keyed by the
                    # first cited evidence_id. Used by the hypothesis
                    # renderer to show ↑/↓ confidence deltas on the
                    # refined view. We pick cited_evidence[0] because
                    # it's the most stable join key — the LLM may
                    # rewrite the headline + reasoning but rarely
                    # swaps the primary cited evidence.
                    pre_refine_map = {}
                    for h in result.hypotheses:
                        if h.cited_evidence:
                            pre_refine_map[h.cited_evidence[0]] = {
                                "rank":       h.rank,
                                "confidence": h.confidence,
                                "confidence_pct": h.confidence_pct,
                            }
                    refined = refine_with_notes(
                        result=result, operator_notes=notes_val,
                    )
                    st.session_state["sre_triage_result"] = refined
                    st.session_state["sre_refine_notes"] = notes_val
                    st.session_state["sre_pre_refine_map"] = pre_refine_map
                    # Persist the refined result too, so a refresh restores
                    # the post-refine view rather than the pre-refine.
                    try:
                        _persist.save_result(refined, user_key=_user_key)
                    except Exception:  # noqa: BLE001
                        pass
                    st.rerun()
                except Exception as ref_err:  # noqa: BLE001
                    render_error(ref_err, context="refining with notes")

    # ---- Action bar ----------------------------------------------------
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
        if st.button(
            "💾 Save snapshot",
            use_container_width=True,
            help="Persist the current view to gs:// or the local "
                 "registry so a refresh re-hydrates it.",
        ):
            try:
                saved_path = _persist.save_result(result, user_key=_user_key)
                if saved_path:
                    st.session_state["sre_status_message"] = (
                        f"Saved snapshot ({len(result.hypotheses)} hypotheses)"
                    )
                else:
                    st.warning(
                        "Persistence backend unavailable; in-session view "
                        "is unaffected."
                    )
            except Exception as se:  # noqa: BLE001
                st.warning(f"Save failed: {se}")


def _render_hypothesis_card(hyp: Hypothesis) -> None:
    """Render one hypothesis as a confidence-bar card with reasoning.

    The HTML is assembled as a single-line string (no embedded newlines)
    because Streamlit's markdown processor treats blank lines inside an
    unsafe_allow_html block as paragraph breaks, which closes the
    wrapping <div> early and renders the inner </div> tags as visible
    text. Single-line emission sidesteps the markdown paragraph parser
    entirely — same fix Streamlit's own components docs recommend for
    multi-element HTML blocks.

    When a refine has run during this session, also shows a small ↑/↓
    delta beside the current confidence — operators see at a glance
    which hypotheses the LLM promoted vs demoted given their notes.
    """
    color = _CONF_COLORS.get(hyp.confidence, "#6b7280")

    # Compute the refine-delta chip if a pre-refine snapshot exists.
    # Match by the first cited evidence_id — the most stable join key
    # because the LLM may rewrite prose but rarely swaps primary
    # citations.
    pre_map = st.session_state.get("sre_pre_refine_map") or {}
    delta_chip = ""
    if pre_map and hyp.cited_evidence:
        pre = pre_map.get(hyp.cited_evidence[0])
        if pre:
            pct_delta = hyp.confidence_pct - pre["confidence_pct"]
            rank_delta = pre["rank"] - hyp.rank  # positive = promoted
            if pct_delta != 0 or rank_delta != 0:
                if pct_delta > 0:
                    arrow_color, arrow = "#00C853", "▲"
                elif pct_delta < 0:
                    arrow_color, arrow = "#EF5350", "▼"
                else:
                    arrow_color, arrow = "#9aa0aa", "●"
                rank_text = (
                    f' · was #{pre["rank"]}' if rank_delta != 0 else ''
                )
                delta_chip = (
                    f'<div style="font-size:0.72em;color:{arrow_color};'
                    f'font-weight:600;margin-top:2px;">'
                    f'{arrow} {pct_delta:+d}%{rank_text}</div>'
                )

    # Build the whole card as one line. Whitespace inside style="" is
    # safe; whitespace BETWEEN tags is what triggers markdown's
    # paragraph-break heuristic, so we strip those.
    bar_html = (
        f'<div style="background:#0E1117;border-radius:6px;height:6px;'
        f'overflow:hidden;margin-top:6px;">'
        f'<div style="background:{color};width:{hyp.confidence_pct}%;'
        f'height:100%;"></div></div>'
    )
    card_html = (
        f'<div style="background:#1A1F2C;border:1px solid #2A3142;'
        f'border-radius:8px;padding:10px 14px;margin-bottom:8px;">'
        f'<div style="display:flex;justify-content:space-between;'
        f'align-items:baseline;gap:12px;">'
        f'<div style="font-weight:600;font-size:0.98em;color:#E5E9F2;'
        f'line-height:1.35;">'
        f'<span style="color:{color};margin-right:6px;">#{hyp.rank}</span>'
        f'{hyp.headline}</div>'
        f'<div style="text-align:right;white-space:nowrap;">'
        f'<div style="font-size:0.8em;color:{color};font-weight:700;">'
        f'{hyp.confidence} · {hyp.confidence_pct}%</div>'
        f'{delta_chip}</div>'
        f'</div>{bar_html}</div>'
    )
    st.markdown(card_html, unsafe_allow_html=True)
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
            # Single-line HTML — see _render_hypothesis_card docstring
            # for why multi-line breaks Streamlit's markdown parser.
            err_html = (
                f'<div style="font-size:0.75em;color:#9aa0aa;margin-top:4px;'
                f'word-break:break-word;">{t.error}</div>' if t.error else ''
            )
            st.markdown(
                f'<div style="background:#1A1F2C;border:1px solid {color}80;'
                f'border-left:4px solid {color};border-radius:6px;'
                f'padding:10px 12px;">'
                f'<div style="font-size:0.75em;color:{color};font-weight:700;'
                f'letter-spacing:0.5px;">{t.status.upper()}</div>'
                f'<div style="font-weight:600;color:#E5E9F2;margin-top:2px;">'
                f'{t.source}</div>'
                f'<div style="font-size:0.8em;color:#9aa0aa;margin-top:2px;">'
                f'{t.item_count} items · {t.duration_ms} ms</div>'
                f'{err_html}</div>',
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
        # Force a rerun so the top-bar Queue metric (rendered ABOVE this
        # button in the page flow) reflects the post-pull state in the
        # same paint cycle. Without this, the metric shows the stale
        # pre-click count until the next interaction triggers a rerun.
        st.rerun()
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
    # Render error-shaped messages as dismissable warnings (sticky
    # captions look like documentation, not transient state). Success
    # messages stay as quiet captions.
    _msg = st.session_state["sre_status_message"]
    _looks_like_error = any(
        kw in _msg.lower() for kw in
        ("not reachable", "failed", "error", "couldn't", "could not")
    )
    if _looks_like_error:
        c_warn, c_dismiss = st.columns([10, 1])
        with c_warn:
            st.warning(_msg, icon="⚠️")
        with c_dismiss:
            if st.button("✕", key="sre_dismiss_status", help="Dismiss"):
                st.session_state["sre_status_message"] = ""
                st.rerun()
    else:
        st.caption(_msg)

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
            # Single-line HTML (markdown-parser fix; see _render_hypothesis_card).
            st.markdown(
                f'<div style="border-left:4px solid {color};'
                f'background-color:#1A1F2C;border-top:{border};'
                f'border-right:{border};border-bottom:{border};'
                f'border-radius:6px;padding:8px 12px;margin-bottom:6px;">'
                f'<div style="font-size:0.75em;color:{color};font-weight:700;'
                f'letter-spacing:0.5px;">{env.severity} · {env.source}</div>'
                f'<div style="font-weight:600;font-size:0.92em;color:#E5E9F2;'
                f'margin-top:2px;line-height:1.3;">{env.policy_name}</div>'
                f'<div style="font-size:0.78em;color:#9aa0aa;margin-top:4px;">'
                f'{env.fired_at}</div></div>',
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
                st.session_state["sre_pre_refine_map"] = None
                st.session_state["sre_refine_notes"] = ""
                # Selecting an alert means we're past the pull step;
                # any "Pub/Sub unreachable" message is stale info now.
                st.session_state["sre_status_message"] = ""


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
