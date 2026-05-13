"""LLM-driven hypothesis narrative rewrite (Phase 8 Day 3).

What this module does
---------------------
Takes the heuristic correlator's output — ranked Hypothesis objects
with templated ``headline`` and ``reasoning`` — and rewrites the
prose in operator-grade English. Scores, ranks, confidence bands,
``cited_evidence``, and ``recommended_actions`` are preserved
verbatim. We only swap the strings the human reads.

Why this separation matters
---------------------------
1. **Cost predictability.** One LLM call per triage regardless of
   how busy the project is. A 1,000-event audit-log window still
   produces at most ``SRE_MAX_HYPOTHESES`` (5) prose rewrites.
2. **Auditability.** Operators can answer "why did this rank #1?"
   from the numeric score breakdown without needing to explain an
   LLM ranking decision. Compliance / change-control reviews like
   that.
3. **Graceful degradation.** If the LLM call fails (429, timeout,
   model misbehaviour producing invalid JSON), the original
   heuristic templates are kept — the triage still ships, just with
   less polished prose.

Call shape
----------
``rewrite(alert, hypotheses, evidence, *, max_attempts=2) -> list[Hypothesis]``

  * ``alert``: AlertEnvelope context (severity, summary,
    resource_refs). The LLM uses this to tailor headlines to the
    specific incident.
  * ``hypotheses``: the heuristic-templated Hypothesis list. Each one
    is rewritten in place — same rank, same cited_evidence, new
    ``headline`` and ``reasoning``.
  * ``evidence``: the full EvidenceItem list so the LLM can look up
    cited evidence by ID. Only the items actually cited by any
    hypothesis are forwarded to the LLM to keep the prompt small.

The function returns the rewritten Hypothesis list. The input list
is NOT mutated — a new list of new Hypothesis instances is returned
so callers can compare before/after if they want to (the UI uses
this for the refine-loop "diff against last run" view).

Prompt design
-------------
The system prompt anchors the LLM as an SRE on-call expert.
The human prompt is a single JSON blob:

    {
      "alert": { ... AlertEnvelope minus raw_payload ... },
      "hypotheses": [
        {
          "rank": 1,
          "current_headline": "...",
          "current_reasoning": ["..."],
          "confidence_pct": 71,
          "cited_evidence_ids": ["asset:0"]
        }, ...
      ],
      "evidence_lookup": {
        "asset:0": { "source": "...", "timestamp": "...", "summary": "..." },
        ...
      }
    }

Response schema (enforced by JSON-mode client + post-validation):

    {
      "hypotheses": [
        { "rank": 1, "headline": "...", "reasoning": ["bullet", ...] },
        ...
      ]
    }

We use rank as the join key, not array order, so a misordered LLM
response still maps correctly. Missing ranks fall back to the original
templated text — no hypothesis ever loses its prose.
"""

from __future__ import annotations

import copy
import json
from typing import Dict, List, Optional

from common.logging import get_logger

from ..results import AlertEnvelope, EvidenceItem, Hypothesis


_log = get_logger(__name__)


# Cap on bullets per hypothesis. Operators skim — past ~5 bullets the
# value-density drops sharply and the UI starts wrapping. The LLM
# tends to write 3-4 when given this hint.
_MAX_REASONING_BULLETS = 5

# Lift heavily-truncated alert fields. raw_payload of a Cloud Monitoring
# alert can be 20-50 KB of nested JSON which doesn't help the rewrite
# and bloats the prompt. We drop it and keep the operator-facing fields.
_ALERT_FIELDS_FOR_PROMPT = (
    "alert_id", "source", "fired_at", "policy_name", "summary",
    "severity", "resource_refs", "project_id", "labels",
)

# Evidence lookup payload trimmed similarly — full audit-log records
# would dwarf the rest of the prompt. We pull only the fields the
# LLM actually uses to reason.
_EVIDENCE_FIELDS_FOR_PROMPT = (
    "source", "timestamp", "change_type",
    "resource_ref", "actor", "summary", "relevance_score",
)


def rewrite(
    *,
    alert: AlertEnvelope,
    hypotheses: List[Hypothesis],
    evidence: List[EvidenceItem],
    extra_context: Optional[str] = None,
) -> List[Hypothesis]:
    """Rewrite each hypothesis's headline + reasoning via the LLM.

    Returns a NEW list of new Hypothesis instances. Input is not mutated.

    Args:
        alert: incident context. Severity + summary anchor the prose
            tone (SEV1 + "5xx error rate spiked" produces blunter
            text than SEV3 + "occasional latency uptick").
        hypotheses: heuristic-templated Hypothesis list. Up to ~5 in
            practice (SRE_MAX_HYPOTHESES caps the correlator output).
        evidence: every EvidenceItem the correlator considered. Only
            the items cited by some hypothesis make it into the prompt.
        extra_context: optional free-text appended to the user prompt.
            Used by the refine loop to inject operator notes.

    Returns:
        New list of Hypothesis with LLM-rewritten ``headline`` +
        ``reasoning``. On any LLM failure, returns deep-copies of the
        input list unchanged — the triage still ships with the
        heuristic templates.
    """
    if not hypotheses:
        return []

    # Deep-copy so we never mutate the caller's list — operator might
    # want to compare before/after in the refine flow.
    out = [copy.deepcopy(h) for h in hypotheses]

    # Try to obtain the LLM client AND the LangChain message classes
    # in one shot. We import both here (not at module top) so a local
    # dev shell without langchain installed can still import this
    # module — it just falls back to the heuristic templates.
    # llm_provider's get_llm_client() raises PreflightError when
    # Vertex AI's init fails (missing ADC, project misconfig, etc.).
    try:
        import llm_provider
        from langchain_core.messages import SystemMessage, HumanMessage
        client = llm_provider.get_llm_client()
    except Exception as e:  # noqa: BLE001
        _log.warning(
            "hypothesis_rewrite_skipped",
            reason="LLM client or langchain unavailable",
            error_type=type(e).__name__,
            error=str(e),
            hint="Falling back to heuristic templates",
        )
        return out

    prompt_messages = _build_prompt(
        alert=alert, hypotheses=hypotheses, evidence=evidence,
        extra_context=extra_context,
        system_message_cls=SystemMessage,
        human_message_cls=HumanMessage,
    )

    try:
        response = llm_provider.safe_invoke(client, prompt_messages)
    except Exception as e:  # noqa: BLE001
        _log.warning(
            "hypothesis_rewrite_llm_failed",
            error_type=type(e).__name__, error=str(e),
            hint="Keeping heuristic templates",
        )
        return out

    rewritten_by_rank = _parse_response(response)
    if not rewritten_by_rank:
        _log.warning(
            "hypothesis_rewrite_parse_failed",
            hint="LLM returned malformed JSON; keeping heuristic templates",
        )
        return out

    # Apply rewrites. Missing ranks keep their original prose — never
    # leave a hypothesis with empty text just because the LLM skipped it.
    applied = 0
    for h in out:
        rw = rewritten_by_rank.get(h.rank)
        if not rw:
            continue
        new_headline = (rw.get("headline") or "").strip()
        new_reasoning = rw.get("reasoning") or []
        if isinstance(new_reasoning, str):
            # Defensive: the LLM occasionally returns a single string
            # instead of an array. Split on bullets / newlines.
            new_reasoning = [
                line.strip(" \t-•").rstrip(".")
                for line in new_reasoning.split("\n")
                if line.strip()
            ]
        new_reasoning = [
            str(b).strip() for b in new_reasoning if str(b).strip()
        ][:_MAX_REASONING_BULLETS]

        if new_headline:
            h.headline = new_headline
        if new_reasoning:
            h.reasoning = new_reasoning
        applied += 1

    _log.info(
        "hypothesis_rewrite_complete",
        alert_id=alert.alert_id,
        hypothesis_count=len(out),
        applied_count=applied,
    )
    return out


# ---------------------------------------------------------------------------
# Prompt construction
# ---------------------------------------------------------------------------


_SYSTEM_PROMPT = """You are an SRE on-call expert writing incident triage narratives.

You receive:
  - An alert that fired in production
  - A ranked list of probable causes already scored by a heuristic
    correlator (temporal proximity + resource overlap + change-type
    weight). Each cause cites specific evidence by ID.
  - A lookup of the cited evidence (audit log entries, deploys, IAM
    changes).

Your job: rewrite each hypothesis's HEADLINE (one sentence) and
REASONING (3-5 short bullet points) in clear, direct English an
on-call engineer would actually read.

Style requirements:
  - Headlines are one sentence, factual, no hedging language. Example:
    "Firewall update on payments-prod-sg 4 minutes before the alert"
  - Reasoning bullets are short (12-20 words each), each makes one
    point. Cite specific evidence IDs from the lookup when relevant.
  - DO NOT invent new evidence. Use only what's in the prompt.
  - DO NOT change ranks, confidence percentages, or cited_evidence IDs.
  - If the evidence is genuinely weak (low relevance score, only
    temporal overlap), say so honestly rather than overclaiming.

You MUST return valid JSON matching this schema exactly:
{
  "hypotheses": [
    { "rank": <int>, "headline": "<string>", "reasoning": ["<bullet>", "..."] }
  ]
}

Return rewrites for every input hypothesis. Use the same rank values
the input provided."""


def _build_prompt(
    *,
    alert: AlertEnvelope,
    hypotheses: List[Hypothesis],
    evidence: List[EvidenceItem],
    extra_context: Optional[str],
    system_message_cls,
    human_message_cls,
) -> List:
    """Construct LangChain messages list for safe_invoke().

    The message classes are passed in (not imported here) so this
    function stays importable in environments without langchain. The
    caller — rewrite() — guarantees both classes are non-None before
    invoking us.
    """
    cited_ids = set()
    for h in hypotheses:
        cited_ids.update(h.cited_evidence)
    evidence_lookup = {
        e.evidence_id: {
            k: getattr(e, k) for k in _EVIDENCE_FIELDS_FOR_PROMPT
        }
        for e in evidence if e.evidence_id in cited_ids
    }

    alert_dict = {k: getattr(alert, k) for k in _ALERT_FIELDS_FOR_PROMPT}

    hypothesis_payload = [
        {
            "rank": h.rank,
            "current_headline": h.headline,
            "current_reasoning": h.reasoning,
            "confidence_pct": h.confidence_pct,
            "cited_evidence_ids": h.cited_evidence,
        }
        for h in hypotheses
    ]

    user_blob = {
        "alert": alert_dict,
        "hypotheses": hypothesis_payload,
        "evidence_lookup": evidence_lookup,
    }
    user_text = (
        "Here is the incident context. Rewrite each hypothesis's "
        "headline and reasoning per the style requirements.\n\n"
        + json.dumps(user_blob, indent=2, default=str)
    )

    if extra_context:
        # Pasted operator notes from the refine flow. We add them
        # AFTER the JSON so the model treats them as supplementary
        # guidance rather than as part of the schema.
        user_text += (
            "\n\nADDITIONAL OPERATOR CONTEXT (consider this when "
            "writing the prose):\n" + extra_context.strip()
        )

    return [
        system_message_cls(content=_SYSTEM_PROMPT),
        human_message_cls(content=user_text),
    ]


# ---------------------------------------------------------------------------
# Response parsing
# ---------------------------------------------------------------------------


def _parse_response(response) -> Dict[int, Dict]:
    """Pull the rewritten-hypothesis dict out of an LLM response.

    Returns a {rank: {headline, reasoning}} mapping. Empty dict on any
    parse failure — caller falls back to heuristic templates.

    Tolerant of:
      * markdown fences around the JSON (some Gemini versions wrap
        despite the response_format=json_object hint)
      * top-level array instead of an object with a "hypotheses" key
      * stray text before/after the JSON object
    """
    raw = getattr(response, "content", None) or str(response)
    text = raw.strip()

    # Strip markdown fences defensively.
    if text.startswith("```"):
        # Drop the opening fence + optional language tag, then the trailing fence.
        text = text.split("\n", 1)[1] if "\n" in text else text
        if text.endswith("```"):
            text = text.rsplit("```", 1)[0]

    text = text.strip()
    if not text:
        return {}

    # Attempt to parse the full string first; on failure, try to find
    # the outermost JSON object via brace matching.
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start == -1 or end == -1 or end <= start:
            return {}
        try:
            data = json.loads(text[start:end + 1])
        except json.JSONDecodeError:
            return {}

    if isinstance(data, list):
        # Some models return a bare array of hypotheses. Wrap it.
        rewrites = data
    elif isinstance(data, dict):
        rewrites = data.get("hypotheses") or data.get("hypothesis") or []
    else:
        return {}

    by_rank: Dict[int, Dict] = {}
    for item in rewrites:
        if not isinstance(item, dict):
            continue
        rank = item.get("rank")
        if rank is None:
            continue
        try:
            rank_int = int(rank)
        except (TypeError, ValueError):
            continue
        by_rank[rank_int] = {
            "headline":  item.get("headline", ""),
            "reasoning": item.get("reasoning", []),
        }

    return by_rank
