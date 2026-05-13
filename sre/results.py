"""SRE engine result dataclasses.

Mirrors the convention used by importer / translator / migrator: every
engine returns its own ``*Result`` dataclass shaped for both the
Streamlit UI and the snapshot-persistence layer.

Key shapes:
  * ``AlertEnvelope``  — normalized incoming alert from any trigger
                         (Cloud Monitoring today; PagerDuty / Datadog
                         later). Pre-parse layer so downstream code
                         doesn't care which provider fired it.
  * ``EvidenceItem``    — one observed change OR signal pulled from a
                         source (asset change, IAM grant, deploy, etc.).
                         Carries everything the correlator needs to
                         score it: timestamp, resource refs, actor,
                         change-type, raw payload.
  * ``Hypothesis``      — ranked probable cause emitted by the
                         correlator + LLM. Cites specific evidence_ids.
  * ``IncidentResult``  — top-level dataclass the engine returns.
                         Carries the alert, every collected evidence
                         item, the ranked hypotheses, timing breakdown
                         per source, and a notes log.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


# Severity tokens (UI + ranking inputs). Tracking PagerDuty's vocabulary
# so Phase-4 webhook ingestion maps 1:1 without translation tables.
SEV1 = "SEV1"
SEV2 = "SEV2"
SEV3 = "SEV3"
SEV4 = "SEV4"
SEV_INFO = "INFO"

SEVERITY_ORDER = (SEV1, SEV2, SEV3, SEV4, SEV_INFO)


# Confidence buckets for hypotheses. Same buckets the existing Migrator
# coverage table uses — keeps the UI tokenization consistent platform-wide.
CONFIDENCE_HIGH    = "HIGH"
CONFIDENCE_MEDIUM  = "MEDIUM"
CONFIDENCE_LOW     = "LOW"

CONFIDENCE_BANDS = (CONFIDENCE_HIGH, CONFIDENCE_MEDIUM, CONFIDENCE_LOW)


@dataclass
class AlertEnvelope:
    """Normalized alert payload, source-agnostic.

    The Pub/Sub puller (or any future trigger) constructs one of these
    from the raw provider payload. Downstream code (correlator, LLM,
    UI) only sees this normalized shape.
    """
    alert_id:           str                    # globally unique; provider-side id
    source:             str                    # "gcp_cloud_monitoring" / "pagerduty" / ...
    fired_at:           str                    # ISO-8601 UTC
    policy_name:        str                    # human-readable policy/alert name
    summary:            str                    # one-line description
    severity:           str = SEV2             # SEV1..SEV4 / INFO
    resource_refs:      List[str] = field(default_factory=list)
    # Resources mentioned by the alert. e.g.
    # ["projects/dev-proj-470211/instances/payments-prod-alb"].
    # Drives correlator's resource-overlap scoring.
    project_id:         Optional[str] = None
    labels:             Dict[str, str] = field(default_factory=dict)
    raw_payload:        Dict[str, Any] = field(default_factory=dict)
    # The provider's original JSON, untouched. Operators can inspect
    # via the "Raw" tab; we never lose source-of-truth.
    pubsub_message_id:  Optional[str] = None
    pubsub_ack_id:      Optional[str] = None
    # Pub/Sub-specific. Needed to ack/nack after triage.


@dataclass
class EvidenceItem:
    """One observed change or signal in the lookback window.

    Sources produce these; the correlator scores them; the LLM cites
    them in hypotheses by ``evidence_id``.
    """
    evidence_id:    str                    # source-prefixed (e.g. "asset:42", "iam:7")
    source:         str                    # "gcp_asset_changes" / "gcp_iam_changes" / "gcp_deploys"
    timestamp:      str                    # ISO-8601 UTC
    change_type:    str                    # "CREATE" / "MODIFY" / "DELETE" / "DEPLOY" / "GRANT" / "REVOKE"
    resource_ref:   str                    # canonical resource path
    actor:          str = ""               # who did it; SA email / human / "system"
    summary:        str = ""               # one-line human-readable
    related_refs:   List[str] = field(default_factory=list)
    # Other resources touched in the same change. Helps correlator
    # follow dep graph (e.g., "SG modified" → "ALB attached to SG").
    relevance_score: float = 0.0           # 0..1; set by correlator
    raw_payload:    Dict[str, Any] = field(default_factory=dict)
    # Provider's original record. Operators inspect via the evidence
    # side-panel; clickable GCP Console deep link is built from this.


@dataclass
class Hypothesis:
    """A ranked probable cause."""
    rank:           int                    # 1 = top
    confidence:     str                    # HIGH / MEDIUM / LOW
    confidence_pct: int                    # 0..100 — drives the gauge bar
    headline:       str                    # one-sentence "what happened"
    reasoning:      List[str] = field(default_factory=list)
    # Plain-English bullets. Operators expand to see why this ranks here.
    cited_evidence: List[str] = field(default_factory=list)
    # evidence_ids the hypothesis cites. UI uses these to filter the
    # evidence pane to just this hypothesis's supporting items.
    recommended_actions: List[Dict[str, str]] = field(default_factory=list)
    # Each: {label, kind, payload}. kind ∈ {"revert_pr", "slack_post",
    # "gcloud_cmd", "runbook"}. UI renders a button per item.


@dataclass
class SourceTiming:
    """Per-source latency / item-count snapshot for the UI source chips.
    Surfaces the "where did the time go" view operators want."""
    source:      str
    item_count:  int = 0
    duration_ms: int = 0
    status:      str = "ok"   # "ok" / "partial" / "failed"
    error:       Optional[str] = None


@dataclass
class IncidentResult:
    """End-to-end triage output.

    A+D contract: returned regardless of per-source outcomes. A source
    that 500s shows up as SourceTiming(status="failed") rather than
    raising — operator still sees the partial picture.
    """
    # Input + context
    alert:           AlertEnvelope
    project_id:      str
    tenant_id:       str = "default"
    lookback_min:    int = 60

    # Evidence + ranking
    evidence:        List[EvidenceItem] = field(default_factory=list)
    hypotheses:      List[Hypothesis] = field(default_factory=list)
    source_timings:  List[SourceTiming] = field(default_factory=list)

    # Bookkeeping
    started_at:      str = ""
    completed_at:    str = ""
    duration_s:      float = 0.0
    notes:           List[str] = field(default_factory=list)
    errors:          List[str] = field(default_factory=list)

    # ---- summary helpers used by UI + snapshot ----

    @property
    def top_hypothesis(self) -> Optional[Hypothesis]:
        return self.hypotheses[0] if self.hypotheses else None

    @property
    def evidence_count(self) -> int:
        return len(self.evidence)

    def as_fields(self) -> Dict[str, Any]:
        """Flat-dict shape for snapshots + structured logging."""
        return {
            "tenant_id":         self.tenant_id,
            "project_id":        self.project_id,
            "alert_id":          self.alert.alert_id,
            "alert_source":      self.alert.source,
            "severity":          self.alert.severity,
            "lookback_min":      self.lookback_min,
            "evidence_count":    self.evidence_count,
            "hypothesis_count":  len(self.hypotheses),
            "top_confidence":    (
                self.top_hypothesis.confidence if self.top_hypothesis else None
            ),
            "duration_s":        self.duration_s,
            "errors":            self.errors,
        }
