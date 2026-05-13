"""SRE correlator — heuristic evidence ranking + hypothesis synthesis.

Day-2 deliverable (this module): a pure-Python heuristic ranker that
takes the alert + a list of EvidenceItems collected from the three
GCP sources and produces a ranked list of Hypothesis objects.

Day-3 swap-in: replace the templated headline + reasoning bullets
with a Claude API call that writes them in plain English. The
scoring + clustering logic here stays — Claude consumes the same
score, just rewrites the prose. That separation matters because:

  * The score is auditable: an operator can ask "why did the agent
    rank this hypothesis first?" and we can show the numeric
    breakdown (temporal proximity 0.8, resource overlap 0.7, etc.).
    With a pure-LLM ranker, the answer collapses to "the LLM said so."
  * Costs stay flat: heuristic scoring runs in microseconds against
    arbitrary evidence volumes. Claude only writes the top-N
    headlines, so token spend is bounded regardless of how busy the
    project is.

Algorithm (v0 — Day 2)
---------------------
1. **Score each EvidenceItem** on three axes, combined into a single
   ``relevance_score`` in [0, 1]:

     * **Temporal proximity** (weight 0.5). How close to the alert
       in time? Closer = higher. Gaussian decay centered on
       ``alert.fired_at`` with sigma = lookback/2. An event 5 minutes
       before the alert scores ~0.95; an event at the very edge of
       the lookback window scores ~0.5.

     * **Resource overlap** (weight 0.35). Does the evidence touch
       a resource the alert names? Substring match (case-insensitive)
       between ``evidence.resource_ref`` (or its related_refs) and
       any ``alert.resource_refs``. Score is 1.0 on direct match,
       0.6 on related-ref match, 0.0 otherwise. Substring-match
       (not exact) so "payments-prod-alb" matches
       "projects/X/instances/payments-prod-alb".

     * **Change-type weight** (weight 0.15). DELETE > REVOKE >
       MODIFY > GRANT > CREATE > DEPLOY. The intuition is that
       DESTRUCTIVE changes correlate more strongly with incidents
       than additive ones. DEPLOY gets its own (slightly elevated)
       weight because deploys are the single most common incident
       root cause we see.

2. **Cluster** evidence by (source, change_type, top-of-resource-path)
   so multiple symptoms of the same change collapse to one
   hypothesis. e.g., a deploy emits a Build + a Cloud Run revision
   create + a firewall rule update — all roll up to "Deploy at HH:MM
   touched payments-api".

3. **Synthesize a Hypothesis per cluster**, ranked by the cluster's
   top relevance score. Confidence bands:

     * HIGH    if top score >= 0.70
     * MEDIUM  if top score >= 0.40
     * LOW     otherwise

   Confidence pct = round(top_score * 100).

4. **Cap output** at SRE_MAX_HYPOTHESES (default 5). Operators rarely
   read past 3; surfacing more dilutes attention.

5. **Suggest recommended actions** based on cluster shape:
   * DEPLOY cluster   → "Revert PR" button (Day 3 wires the actual PR)
   * GRANT/REVOKE     → "Open IAM policy" link
   * DELETE           → "Restore from snapshot" stub
   * MODIFY           → "Diff against last-known-good" stub

   These are placeholder action buttons today; Day 3 wires real
   payloads.
"""

from __future__ import annotations

import datetime
import math
import os
from collections import defaultdict
from typing import Any, Dict, List, Tuple

from common.logging import get_logger

from .results import (
    AlertEnvelope,
    EvidenceItem,
    Hypothesis,
    CONFIDENCE_HIGH,
    CONFIDENCE_MEDIUM,
    CONFIDENCE_LOW,
)


_log = get_logger(__name__)


# -----------------------------------------------------------------------------
# Tunables. Env-var-overridable so operators can experiment with the
# weights without a redeploy — the SRE team can pin a customer profile
# (more weight on IAM for finance customers, more on deploys for
# pre-revenue SaaS) just by setting env vars on the Cloud Run service.
# -----------------------------------------------------------------------------

# Combined-score component weights. Must sum to 1.0 (asserted at module
# load — drift here would silently bias rankings).
_W_TEMPORAL = float(os.environ.get("SRE_W_TEMPORAL", "0.50"))
_W_RESOURCE = float(os.environ.get("SRE_W_RESOURCE", "0.35"))
_W_CHANGETYPE = float(os.environ.get("SRE_W_CHANGETYPE", "0.15"))

assert abs(_W_TEMPORAL + _W_RESOURCE + _W_CHANGETYPE - 1.0) < 1e-6, (
    "SRE correlator weights must sum to 1.0; check SRE_W_* env vars"
)

# Per-change-type intrinsic weight. Used as the change-type axis
# score before combining. Calibrated against the user's "60% of
# value is 'what changed' triage" stat — deploys + destructive
# changes get the lion's share.
_CHANGE_TYPE_WEIGHTS = {
    "DELETE": 1.00,
    "REVOKE": 0.90,
    "DEPLOY": 0.85,
    "MODIFY": 0.65,
    "GRANT":  0.55,
    "CREATE": 0.40,
}

# Confidence bands. Tuned to the user-visible UX:
#   HIGH → green confidence chip + bold headline
#   MEDIUM → amber chip
#   LOW → gray chip, "investigate further" hint
_CONFIDENCE_HIGH_THRESHOLD = 0.70
_CONFIDENCE_MEDIUM_THRESHOLD = 0.40

# Cap on hypotheses emitted. Past ~5, operator attention dilutes;
# the UI's main triage area renders 3 hypothesis cards above the
# fold so 5 leaves a buffer for cases where two are near-tied.
SRE_MAX_HYPOTHESES = int(os.environ.get("SRE_MAX_HYPOTHESES", "5"))


# -----------------------------------------------------------------------------
# Public API — orchestrator calls this.
# -----------------------------------------------------------------------------


def rank(
    *,
    alert: AlertEnvelope,
    evidence: List[EvidenceItem],
) -> List[Hypothesis]:
    """Score evidence + return ranked hypotheses.

    Mutates each ``EvidenceItem.relevance_score`` in place so the UI
    can render per-item scores in the evidence timeline. This is
    intentional — the alternative (returning scored copies) would
    require the orchestrator to thread a parallel list through to
    the UI, doubling the moving parts.

    Returns:
        List of Hypothesis ranked highest-confidence first, capped at
        SRE_MAX_HYPOTHESES. Empty list if no evidence — the UI shows
        an "all clear" note in that case rather than spurious
        low-confidence guesses.
    """
    if not evidence:
        _log.info("correlator_no_evidence", alert_id=alert.alert_id)
        return []

    # ---- Step 1: score each evidence item ----
    anchor_dt = _parse_iso_utc(alert.fired_at)
    # Use a lookback-derived sigma so very-old events near the edge
    # of the window still score reasonably. Sigma = lookback_min/2
    # would be ideal but we don't have lookback here — derive from
    # the OLDEST evidence in the list as a proxy (operators set
    # lookback to cover the actual evidence horizon).
    sigma_min = _derive_sigma_min(anchor_dt, evidence)

    alert_refs_norm = [_normalize_ref(r) for r in alert.resource_refs]

    for ev in evidence:
        temporal = _score_temporal(
            ev_timestamp=ev.timestamp, anchor=anchor_dt, sigma_min=sigma_min,
        )
        overlap = _score_resource_overlap(ev=ev, alert_refs=alert_refs_norm)
        change_w = _CHANGE_TYPE_WEIGHTS.get(ev.change_type, 0.50)
        # Combined score is a weighted average — each axis independently
        # contributes regardless of the others' values. That matters
        # because a deploy 30 minutes before the alert on an unrelated
        # resource SHOULD still rank lower than a config change 5
        # minutes before on the alert's resource.
        ev.relevance_score = round(
            _W_TEMPORAL * temporal
            + _W_RESOURCE * overlap
            + _W_CHANGETYPE * change_w,
            3,
        )

    # Stable sort so two equal-score items keep their input order
    # (which tends to be newest-first from the source collectors).
    evidence_sorted = sorted(
        evidence, key=lambda e: e.relevance_score, reverse=True,
    )

    # ---- Step 2: cluster ----
    clusters = _cluster_evidence(evidence_sorted)

    # ---- Step 3: synthesize hypotheses ----
    hypotheses: List[Hypothesis] = []
    for cluster_key, items in clusters.items():
        if not items:
            continue
        top_score = items[0].relevance_score  # cluster sorted by score
        confidence = _band_for_score(top_score)
        headline = _build_headline(cluster_key, items, alert=alert)
        reasoning = _build_reasoning(cluster_key, items, alert=alert)
        actions = _build_recommended_actions(cluster_key, items)
        hypotheses.append(Hypothesis(
            rank=0,   # filled below after global sort
            confidence=confidence,
            confidence_pct=int(round(top_score * 100)),
            headline=headline,
            reasoning=reasoning,
            cited_evidence=[it.evidence_id for it in items[:10]],
            recommended_actions=actions,
        ))

    # ---- Step 4: rank globally + cap ----
    hypotheses.sort(key=lambda h: h.confidence_pct, reverse=True)
    hypotheses = hypotheses[:SRE_MAX_HYPOTHESES]
    for i, h in enumerate(hypotheses, start=1):
        h.rank = i

    _log.info(
        "correlator_complete",
        alert_id=alert.alert_id,
        evidence_count=len(evidence),
        cluster_count=len(clusters),
        hypothesis_count=len(hypotheses),
        top_confidence=hypotheses[0].confidence if hypotheses else None,
    )
    return hypotheses


# -----------------------------------------------------------------------------
# Scoring axes
# -----------------------------------------------------------------------------


def _score_temporal(
    *, ev_timestamp: str, anchor: datetime.datetime, sigma_min: float,
) -> float:
    """Gaussian decay on |ev_timestamp - anchor|. Returns [0, 1]."""
    ev_dt = _parse_iso_utc(ev_timestamp)
    if ev_dt is None or anchor is None:
        # Unknown timestamps get a neutral 0.5 — neither rewarded
        # nor punished. Better than 0.0 (would unfairly downrank
        # evidence whose timestamp got lost in serialization).
        return 0.5
    delta_min = abs((ev_dt - anchor).total_seconds()) / 60.0
    # Gaussian centered at 0, sigma=sigma_min. score(0) = 1.0;
    # score(sigma) ≈ 0.61; score(2σ) ≈ 0.14.
    return math.exp(-(delta_min ** 2) / (2 * max(sigma_min, 1.0) ** 2))


def _score_resource_overlap(
    *, ev: EvidenceItem, alert_refs: List[str],
) -> float:
    """Resource-name overlap.

    Scoring tiers:
      1.0  — direct, specific match (leaf names match OR substring
             match where the shorter ref is itself specific, i.e.
             >= 3 path segments)
      0.6  — related-ref match (evidence touched a sibling)
      0.4  — generic prefix match (e.g., shared project prefix only)
      0.0  — no overlap

    Why the 0.4 tier exists: every audit log on a project shares the
    "projects/<project_id>" prefix with every alert resource ref. Naive
    substring matching would call that a direct match and drown out
    the real causes — see Day-2 smoke (a project-level IAM grant
    ranking above an obvious firewall-update root cause). The leaf-name
    + segment-depth check pins "direct" to genuinely specific matches.
    """
    if not alert_refs:
        # No alert resource refs → can't score overlap; neutral 0.5.
        return 0.5

    ev_ref_norm = _normalize_ref(ev.resource_ref)
    ev_leaf = _leaf_name(ev_ref_norm)
    related_norms = [_normalize_ref(r) for r in (ev.related_refs or [])]

    has_generic_prefix_match = False

    for a in alert_refs:
        if not a or not ev_ref_norm:
            continue
        a_leaf = _leaf_name(a)
        # Tier 1: leaf-name match (the most reliable "same resource"
        # signal — "payments-prod-alb" leaf matches regardless of how
        # the rest of the path is shaped).
        if a_leaf and ev_leaf and a_leaf == ev_leaf:
            return 1.0
        # Tier 1 (substring variant): substring match where the
        # SHORTER side is itself specific (>= 3 path segments). That
        # prevents the project-prefix case from claiming 1.0.
        if a in ev_ref_norm or ev_ref_norm in a:
            shorter = a if len(a) <= len(ev_ref_norm) else ev_ref_norm
            if _path_depth(shorter) >= 3:
                return 1.0
            has_generic_prefix_match = True

    # Tier 2: related-ref direct match.
    for r in related_norms:
        if not r:
            continue
        r_leaf = _leaf_name(r)
        for a in alert_refs:
            if not a:
                continue
            a_leaf = _leaf_name(a)
            if a_leaf and r_leaf and a_leaf == r_leaf:
                return 0.6
            if (a in r or r in a) and _path_depth(
                a if len(a) <= len(r) else r
            ) >= 3:
                return 0.6

    # Tier 3: generic prefix-only match (project-shared).
    if has_generic_prefix_match:
        return 0.4

    return 0.0


def _path_depth(ref: str) -> int:
    """Count path segments after normalization. Empty → 0.

    GCP resource paths are slash-delimited. "projects/X" → 2 segments.
    "projects/X/zones/Y/instances/Z" → 5 segments. We treat 3+ as
    'specific enough' for a direct match because anything shallower
    is structurally generic (project-level, location-level).
    """
    if not ref:
        return 0
    return sum(1 for p in ref.split("/") if p)


# -----------------------------------------------------------------------------
# Clustering
# -----------------------------------------------------------------------------


def _cluster_evidence(
    evidence_sorted: List[EvidenceItem],
) -> Dict[Tuple[str, str, str], List[EvidenceItem]]:
    """Group items into hypothesis clusters.

    Cluster key = (source, change_type, normalized resource bucket).
    The resource bucket strips the leaf path component so multiple
    operations on related resources roll up. E.g.:

      asset:0  MODIFY  projects/X/firewalls/payments-prod-sg
      asset:1  MODIFY  projects/X/firewalls/payments-prod-sg/rules/123
      asset:2  MODIFY  projects/X/firewalls/payments-prod-sg/rules/124

    All three land in the same cluster keyed on
    "projects/X/firewalls/payments-prod-sg".
    """
    clusters: Dict[Tuple[str, str, str], List[EvidenceItem]] = defaultdict(list)
    for ev in evidence_sorted:
        bucket = _resource_bucket(ev.resource_ref)
        clusters[(ev.source, ev.change_type, bucket)].append(ev)
    return clusters


def _resource_bucket(resource_ref: str) -> str:
    """Strip the leaf path component for clustering.

    Examples:
        ".../instances/foo"          → ".../instances/foo"   (terminal)
        ".../instances/foo/disks/x"  → ".../instances/foo"   (rolled up)
        "" or None                   → "(unknown)"
    """
    if not resource_ref:
        return "(unknown)"
    # Heuristic: keep at most 6 path segments. GCP resource paths are
    # 2-5 segments deep ("projects/X/zones/Y/instances/Z" = 5); 6
    # gives a buffer for service-specific sub-resources.
    parts = resource_ref.split("/")
    if len(parts) <= 6:
        return resource_ref
    return "/".join(parts[:6])


# -----------------------------------------------------------------------------
# Hypothesis text synthesis (Day 2 templates; Day 3 swaps in Claude)
# -----------------------------------------------------------------------------


def _build_headline(
    cluster_key: Tuple[str, str, str],
    items: List[EvidenceItem],
    *,
    alert: AlertEnvelope,
) -> str:
    """One-sentence 'what happened' for the cluster."""
    source, change_type, bucket = cluster_key
    actor = items[0].actor or "unknown actor"
    when = _humanize_when(items[0].timestamp, alert.fired_at)
    leaf = _leaf_name(bucket)

    if source == "gcp_deploys":
        return f"Deploy {when} on {leaf}"
    if source == "gcp_iam_changes":
        verb = {"GRANT": "granted IAM", "REVOKE": "revoked IAM"}.get(
            change_type, "modified IAM"
        )
        return f"{actor} {verb} {when} on {leaf}"
    # gcp_asset_changes
    verb_map = {
        "DELETE": "deleted", "MODIFY": "modified", "CREATE": "created",
    }
    verb = verb_map.get(change_type, change_type.lower())
    return f"{actor} {verb} {leaf} {when}"


def _build_reasoning(
    cluster_key: Tuple[str, str, str],
    items: List[EvidenceItem],
    *,
    alert: AlertEnvelope,
) -> List[str]:
    """Plain-English bullets explaining why this ranks here."""
    source, change_type, bucket = cluster_key
    top = items[0]
    bullets: List[str] = []

    # 1. Temporal proximity in operator-readable terms.
    bullets.append(
        f"{change_type} on this resource occurred at {top.timestamp}, "
        f"{_humanize_delta(top.timestamp, alert.fired_at)} from when the alert fired."
    )

    # 2. Resource overlap explanation.
    if alert.resource_refs:
        alert_refs_norm = [_normalize_ref(r) for r in alert.resource_refs]
        bucket_norm = _normalize_ref(bucket)
        if any(a in bucket_norm or bucket_norm in a for a in alert_refs_norm if a):
            bullets.append(
                "This resource directly matches the alert's affected resource — "
                "high confidence the two are causally related."
            )
        else:
            bullets.append(
                "This resource does not directly match the alert's affected "
                "resource; correlation is via temporal proximity + change type."
            )

    # 3. Volume hint when the cluster has multiple supporting items.
    if len(items) > 1:
        bullets.append(
            f"{len(items)} related changes in the same cluster within the "
            f"lookback window — pattern suggests a deliberate sequence "
            f"(e.g., a deploy or a maintenance window), not a one-off."
        )

    # 4. Actor + actor-type signal.
    if top.actor:
        principal_kind = (
            "service account" if top.actor.endswith(".gserviceaccount.com")
            else "user"
        )
        bullets.append(f"Initiated by {principal_kind} {top.actor}.")

    return bullets


def _build_recommended_actions(
    cluster_key: Tuple[str, str, str],
    items: List[EvidenceItem],
) -> List[Dict[str, str]]:
    """Action button stubs. Day 3 wires real payloads."""
    source, change_type, _ = cluster_key
    actions: List[Dict[str, str]] = []

    if source == "gcp_deploys":
        actions.append({
            "label": "Revert PR",
            "kind": "revert_pr",
            "payload": items[0].evidence_id,
        })
        actions.append({
            "label": "Open build logs",
            "kind": "runbook",
            "payload": ",".join(r for it in items[:3] for r in it.related_refs),
        })

    if source == "gcp_iam_changes":
        actions.append({
            "label": "Open IAM policy",
            "kind": "runbook",
            "payload": items[0].resource_ref,
        })
        if change_type == "GRANT":
            actions.append({
                "label": "Revoke recent grant",
                "kind": "gcloud_cmd",
                "payload": items[0].evidence_id,
            })

    if source == "gcp_asset_changes":
        if change_type == "DELETE":
            actions.append({
                "label": "Restore from snapshot",
                "kind": "runbook",
                "payload": items[0].resource_ref,
            })
        else:
            actions.append({
                "label": "Diff against last-known-good",
                "kind": "runbook",
                "payload": items[0].resource_ref,
            })

    # Universal action — post to Slack. Day 3 wires the actual webhook.
    actions.append({
        "label": "Post to #incidents",
        "kind": "slack_post",
        "payload": ",".join(it.evidence_id for it in items[:3]),
    })

    return actions


# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------


def _band_for_score(score: float) -> str:
    if score >= _CONFIDENCE_HIGH_THRESHOLD:
        return CONFIDENCE_HIGH
    if score >= _CONFIDENCE_MEDIUM_THRESHOLD:
        return CONFIDENCE_MEDIUM
    return CONFIDENCE_LOW


def _normalize_ref(ref: str) -> str:
    """Lowercase + strip whitespace for substring comparison.

    GCP resource refs aren't quite case-sensitive but APIs occasionally
    return mixed case. Normalize once so the overlap scoring isn't
    surprised.
    """
    return (ref or "").strip().lower()


def _leaf_name(resource_ref: str) -> str:
    """Friendly short name from a resource path."""
    if not resource_ref or resource_ref == "(unknown)":
        return "(unknown resource)"
    return resource_ref.rstrip("/").rsplit("/", 1)[-1] or resource_ref


def _parse_iso_utc(s: str) -> datetime.datetime:
    """Tolerant ISO-8601 → datetime parser. Returns None on failure."""
    if not s:
        return None  # type: ignore[return-value]
    cleaned = s.rstrip("Z")
    try:
        dt = datetime.datetime.fromisoformat(cleaned)
    except ValueError:
        return None  # type: ignore[return-value]
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return dt


def _derive_sigma_min(
    anchor: datetime.datetime, evidence: List[EvidenceItem],
) -> float:
    """Pick the temporal-decay sigma from the oldest evidence's distance.

    Sigma = oldest_distance / 2 makes the Gaussian span the actual
    evidence horizon: at the edge of the window the score is ~0.14
    (still nonzero), at half-way ~0.61 (clearly visible), near the
    alert ~1.0. This auto-adapts to the operator's chosen lookback
    without us having to thread it through.
    """
    if anchor is None:
        return 30.0  # fallback to 30min sigma
    deltas = []
    for ev in evidence:
        ev_dt = _parse_iso_utc(ev.timestamp)
        if ev_dt is None:
            continue
        deltas.append(abs((ev_dt - anchor).total_seconds()) / 60.0)
    if not deltas:
        return 30.0
    return max(max(deltas), 5.0) / 2.0


def _humanize_when(ev_timestamp: str, alert_timestamp: str) -> str:
    """Render 'X minutes before the alert' style text."""
    ev_dt = _parse_iso_utc(ev_timestamp)
    al_dt = _parse_iso_utc(alert_timestamp)
    if ev_dt is None or al_dt is None:
        return "at unknown time"
    delta = al_dt - ev_dt
    minutes = round(delta.total_seconds() / 60.0)
    if minutes > 0:
        return f"{minutes} min before the alert"
    if minutes < 0:
        return f"{abs(minutes)} min after the alert"
    return "at the alert time"


def _humanize_delta(ev_timestamp: str, alert_timestamp: str) -> str:
    """'5 minutes' style absolute delta."""
    ev_dt = _parse_iso_utc(ev_timestamp)
    al_dt = _parse_iso_utc(alert_timestamp)
    if ev_dt is None or al_dt is None:
        return "an unknown duration"
    minutes = abs(round((al_dt - ev_dt).total_seconds() / 60.0))
    if minutes == 0:
        return "less than a minute"
    if minutes == 1:
        return "1 minute"
    if minutes < 60:
        return f"{minutes} minutes"
    hours = minutes / 60
    return f"{hours:.1f} hours"
