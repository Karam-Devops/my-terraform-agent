"""LLM-driven refine-with-notes loop (Phase 8 Day 3).

What this module does
---------------------
After the initial triage, the operator may have context the agent
couldn't see — for example:

  * "I just rolled back the deploy and the alert is still firing"
    → de-emphasize the deploy hypothesis, surface other root causes.
  * "We were doing scheduled maintenance on that SG; ignore it"
    → drop the firewall hypothesis entirely.
  * "Customer reports the issue started ~10 min earlier than the alert"
    → widen the temporal-proximity scoring.

This module wires the "Refine with Claude" button in the UI to a
single LLM call that produces a REFINED IncidentResult:

  * Same alert + same evidence (we don't re-query GCP; that would
    cost time + might surface fresh, unrelated changes).
  * Same number of hypotheses by default, but the LLM may reorder
    them, drop one, or merge two if the operator's notes warrant.
  * Re-written headlines + reasoning that reflect the new context.

Why this is a separate module from hypothesis_writer
----------------------------------------------------
hypothesis_writer's job is narrowly "rewrite the prose" — preserve
ranks, confidence percentages, citations. Refine's job is broader —
the LLM can change ranks AND prose. Two different prompts, two
different output schemas, two different graceful-degradation paths
(refine can fall back to the un-refined result; hypothesis_writer
falls back to heuristic templates).
"""

from __future__ import annotations

import copy
import json
from typing import Dict, List, Optional

from common.logging import get_logger

from ..results import (
    AlertEnvelope,
    EvidenceItem,
    Hypothesis,
    IncidentResult,
    CONFIDENCE_HIGH,
    CONFIDENCE_MEDIUM,
    CONFIDENCE_LOW,
)


_log = get_logger(__name__)


_REFINE_SYSTEM_PROMPT = """You are an SRE on-call expert refining an
incident triage based on new operator-supplied context.

You receive:
  - The original alert
  - The evidence the heuristic correlator collected from GCP audit
    logs (asset changes, IAM changes, deploys)
  - The current ranked hypotheses
  - OPERATOR NOTES — fresh context the operator just typed in.
    Treat these as ground truth: if the operator says "I rolled
    back X", believe them.

Your job: produce a REFINED ranked list of hypotheses that
reflects the operator's notes. You may:
  - Reorder the hypotheses (most likely cause first)
  - Adjust confidence percentages and bands
    (HIGH >= 70, MEDIUM 40-69, LOW < 40)
  - Drop hypotheses the operator's notes have ruled out
  - Merge two hypotheses if they're the same root cause
  - Rewrite headlines and reasoning to reflect the new context
  - Keep cited_evidence sets coherent (use evidence_ids from
    the input only — DO NOT invent new evidence)

Constraints:
  - DO NOT add hypotheses that aren't supported by the evidence
  - DO NOT invent new evidence_ids
  - Keep at most 5 hypotheses (operators read top-3 in practice)
  - If the operator's notes eliminate every hypothesis, return
    an empty list — the UI shows an "all explanations ruled out;
    investigate further" message.

You MUST return valid JSON matching this schema exactly:
{
  "hypotheses": [
    {
      "rank": <int starting at 1>,
      "confidence_pct": <int 0-100>,
      "headline": "<string>",
      "reasoning": ["<bullet>", "..."],
      "cited_evidence": ["<evidence_id>", ...]
    }
  ],
  "summary_note": "<one-line explanation of what changed and why>"
}

Use only evidence_ids that appear in the input. Use rank values
1..N where N is the number of hypotheses in your output."""


def refine_with_notes(
    *,
    result: IncidentResult,
    operator_notes: str,
) -> IncidentResult:
    """Re-rank + rewrite hypotheses given fresh operator context.

    Args:
        result: the current IncidentResult (from a prior triage run).
            Not mutated — a new IncidentResult is returned.
        operator_notes: free-text from the UI. Stripped + capped at
            4000 chars to keep prompt size predictable; empty notes
            are a no-op (returns the original result unchanged).

    Returns:
        A NEW IncidentResult with potentially-reordered hypotheses
        and a note appended to ``result.notes`` describing what the
        refinement changed. Same alert, same evidence, same source
        timings — only the hypotheses + notes shift.

    Falls back to a deep-copy of the original result on any LLM
    failure. The UI doesn't lose any information; the refine button
    just becomes a no-op for that one click.
    """
    notes = (operator_notes or "").strip()[:4000]
    if not notes:
        return result

    refined = copy.deepcopy(result)

    if not refined.hypotheses:
        # Nothing to refine — but stash the note anyway so the
        # operator's input isn't silently dropped.
        refined.notes.append(f"refine: ignored (no hypotheses): {notes[:120]}")
        return refined

    # Same import-guard pattern hypothesis_writer uses — both LLM
    # surfaces degrade identically when the platform's Vertex AI
    # plumbing isn't reachable.
    try:
        import llm_provider
        from langchain_core.messages import SystemMessage, HumanMessage
        client = llm_provider.get_llm_client()
    except Exception as e:  # noqa: BLE001
        _log.warning(
            "refine_skipped",
            reason="LLM client or langchain unavailable",
            error_type=type(e).__name__, error=str(e),
        )
        refined.notes.append(
            f"refine: LLM unavailable; operator notes captured but not applied: "
            f"{notes[:120]}"
        )
        return refined

    messages = _build_prompt(
        alert=result.alert,
        hypotheses=result.hypotheses,
        evidence=result.evidence,
        operator_notes=notes,
        system_message_cls=SystemMessage,
        human_message_cls=HumanMessage,
    )

    try:
        response = llm_provider.safe_invoke(client, messages)
    except Exception as e:  # noqa: BLE001
        _log.warning(
            "refine_llm_failed",
            error_type=type(e).__name__, error=str(e),
        )
        refined.notes.append(
            f"refine: LLM call failed; keeping original ranking. Note: {notes[:120]}"
        )
        return refined

    parsed = _parse_refine_response(response)
    if parsed is None:
        _log.warning("refine_parse_failed")
        refined.notes.append(
            f"refine: LLM response unparseable; keeping original. Note: {notes[:120]}"
        )
        return refined

    new_hyps, summary_note = parsed

    # Validate cited_evidence against the actual evidence set so the
    # LLM can't insert phantom evidence IDs that would mislead the UI.
    valid_evidence_ids = {e.evidence_id for e in result.evidence}
    for h in new_hyps:
        h.cited_evidence = [
            eid for eid in h.cited_evidence if eid in valid_evidence_ids
        ]

    # Regenerate recommended_actions (Day-4 fix). The LLM doesn't write
    # action buttons — those are derived from the dominant cluster shape
    # of the cited evidence (DEPLOY → Revert PR + Open build logs, GRANT
    # → Open IAM policy + Revoke recent grant, etc.). Without this step,
    # refined hypotheses lost their action buttons entirely, leaving the
    # operator with no clickable next-step UX.
    from ..correlator import build_actions_for_hypothesis
    for h in new_hyps:
        h.recommended_actions = build_actions_for_hypothesis(h, result.evidence)

    refined.hypotheses = new_hyps
    refined.notes.append(f"refine: {summary_note}" if summary_note else
                         "refine: hypotheses re-ranked per operator notes")
    refined.notes.append(f"operator_note: {notes[:200]}")

    _log.info(
        "refine_complete",
        alert_id=result.alert.alert_id,
        original_count=len(result.hypotheses),
        refined_count=len(new_hyps),
    )
    return refined


# ---------------------------------------------------------------------------
# Prompt + response handling
# ---------------------------------------------------------------------------


def _build_prompt(
    *,
    alert: AlertEnvelope,
    hypotheses: List[Hypothesis],
    evidence: List[EvidenceItem],
    operator_notes: str,
    system_message_cls,
    human_message_cls,
) -> List:
    """Compose the refine prompt. Same dependency-injection pattern
    hypothesis_writer uses so this stays importable without langchain."""

    user_blob = {
        "alert": {
            "alert_id":      alert.alert_id,
            "fired_at":      alert.fired_at,
            "policy_name":   alert.policy_name,
            "summary":       alert.summary,
            "severity":      alert.severity,
            "resource_refs": alert.resource_refs,
            "labels":        alert.labels,
        },
        "current_hypotheses": [
            {
                "rank":           h.rank,
                "confidence":     h.confidence,
                "confidence_pct": h.confidence_pct,
                "headline":       h.headline,
                "reasoning":      h.reasoning,
                "cited_evidence": h.cited_evidence,
            }
            for h in hypotheses
        ],
        "evidence": [
            {
                "evidence_id":     e.evidence_id,
                "source":          e.source,
                "timestamp":       e.timestamp,
                "change_type":     e.change_type,
                "resource_ref":    e.resource_ref,
                "actor":           e.actor,
                "summary":         e.summary,
                "relevance_score": e.relevance_score,
            }
            for e in evidence
        ],
    }

    user_text = (
        "Here is the current triage. Refine it based on the operator "
        "notes that follow the JSON block.\n\n"
        + json.dumps(user_blob, indent=2, default=str)
        + "\n\n--- OPERATOR NOTES ---\n"
        + operator_notes
    )

    return [
        system_message_cls(content=_REFINE_SYSTEM_PROMPT),
        human_message_cls(content=user_text),
    ]


def _parse_refine_response(response):
    """Pull (hypotheses_list, summary_note) out of an LLM response.

    Returns None on any parse failure so caller can fall back. The
    hypotheses list contains fresh Hypothesis instances with rank
    re-numbered 1..N regardless of what the LLM said.
    """
    raw = getattr(response, "content", None) or str(response)
    text = raw.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else text
        if text.endswith("```"):
            text = text.rsplit("```", 1)[0]
    text = text.strip()
    if not text:
        return None

    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start == -1 or end <= start:
            return None
        try:
            data = json.loads(text[start:end + 1])
        except json.JSONDecodeError:
            return None

    if not isinstance(data, dict):
        return None
    raw_hyps = data.get("hypotheses")
    if not isinstance(raw_hyps, list):
        return None

    summary_note = str(data.get("summary_note", "")).strip()
    hyps: List[Hypothesis] = []
    for i, item in enumerate(raw_hyps, start=1):
        if not isinstance(item, dict):
            continue
        try:
            confidence_pct = int(item.get("confidence_pct", 0))
        except (TypeError, ValueError):
            confidence_pct = 0
        confidence_pct = max(0, min(100, confidence_pct))
        if confidence_pct >= 70:
            confidence_band = CONFIDENCE_HIGH
        elif confidence_pct >= 40:
            confidence_band = CONFIDENCE_MEDIUM
        else:
            confidence_band = CONFIDENCE_LOW

        reasoning = item.get("reasoning") or []
        if isinstance(reasoning, str):
            reasoning = [
                line.strip(" \t-•").rstrip(".")
                for line in reasoning.split("\n") if line.strip()
            ]
        reasoning = [str(r).strip() for r in reasoning if str(r).strip()][:5]

        cited = item.get("cited_evidence") or []
        if not isinstance(cited, list):
            cited = []
        cited = [str(c) for c in cited if c]

        hyps.append(Hypothesis(
            rank=i,   # always 1..N regardless of what the LLM said
            confidence=confidence_band,
            confidence_pct=confidence_pct,
            headline=str(item.get("headline", "")).strip(),
            reasoning=reasoning,
            cited_evidence=cited,
            recommended_actions=[],  # refined hypotheses don't get auto-actions;
                                     # Day-4 wires action regeneration after refine.
        ))

    return (hyps[:5], summary_note)
