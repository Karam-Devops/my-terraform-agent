"""Non-IAM resource-change evidence source (Phase 8 Day 2).

What this source produces
-------------------------
Every Admin Activity audit log entry that represents a CRUD operation
on a GCP resource within the lookback window, EXCEPT IAM events
(those go to ``gcp_iam_changes``). Compute instance starts/stops,
firewall rule edits, GCS bucket creation, GKE cluster scaling — all
land here.

Why audit logs (not Cloud Asset Inventory's change history)
-----------------------------------------------------------
Cloud Asset Inventory's ``BatchGetAssetsHistory`` returns time-windowed
snapshots, BUT requires the caller to pre-name the assets — useless
for an open-ended "what changed in the last hour?" question. Admin
Activity logs are the canonical source for "any change anywhere" and
are always-on / free.

The filter
----------
Match anything that LOOKS like a CRUD method, exclude IAM:

  protoPayload.methodName=~ ".+\\.(insert|create|update|patch|delete|setMetadata|stop|start|resize|setLabels)$"
  AND NOT protoPayload.serviceName="iam.googleapis.com"
  AND NOT protoPayload.methodName="SetIamPolicy"

That's a heuristic — there are services (e.g. logging.googleapis.com,
monitoring.googleapis.com) whose own writes match this pattern but
aren't usually root causes for product incidents. We let them through
and let the correlator down-rank them (admin-API self-writes have low
resource-overlap with product-resource alerts).
"""

from __future__ import annotations

from typing import List

from common.logging import get_logger

from ..results import AlertEnvelope, EvidenceItem
from . import _log_client


_log = get_logger(__name__)


# Methods we treat as "a resource changed". Suffix match keeps the
# filter readable + matches every API version (v1, v1beta1, etc.).
_CHANGE_METHOD_SUFFIXES = (
    "insert",      # most APIs (Create)
    "create",      # some newer APIs (GCE, GCS)
    "update",
    "patch",
    "delete",
    "setMetadata",
    "stop", "start",
    "resize",
    "setLabels",
)

# Method-name → change_type mapping for EvidenceItem.change_type.
# Order matters in the lookup: longer suffix first so "setMetadata"
# matches before "set".
_METHOD_TO_CHANGE_TYPE = (
    ("delete",      "DELETE"),
    ("insert",      "CREATE"),
    ("create",      "CREATE"),
    ("update",      "MODIFY"),
    ("patch",       "MODIFY"),
    ("setMetadata", "MODIFY"),
    ("setLabels",   "MODIFY"),
    ("stop",        "MODIFY"),
    ("start",       "MODIFY"),
    ("resize",      "MODIFY"),
)


def collect(
    *,
    alert: AlertEnvelope,
    project_id: str,
    lookback_min: int,
) -> List[EvidenceItem]:
    """Collect non-IAM resource changes inside the alert's lookback window.

    Args:
        alert: anchors the time window via ``alert.fired_at``.
        project_id: GCP project to query.
        lookback_min: how far back from alert.fired_at to scan.

    Returns:
        List of EvidenceItem with ``source="gcp_asset_changes"`` and
        ``evidence_id`` prefixed ``asset:<n>``. Empty list if no
        changes — or if the logging API isn't enabled / SA lacks
        permission (orchestrator records that as ``status="failed"``).
    """
    start_iso, end_iso = _log_client.compute_window(
        fired_at_iso=alert.fired_at, lookback_min=lookback_min,
    )

    # Method-name disjunction over CRUD suffixes. We use the gcloud
    # logging filter ``:`` (substring-contains) operator joined with
    # ``OR``, rather than a regex with alternation pipes.
    #
    # WHY NOT REGEX: the natural form would be
    #   protoPayload.methodName=~".*\.(insert|create|update|...)$"
    # but the `|` character is the cmd.exe pipe operator on Windows.
    # subprocess.Popen invoking gcloud.cmd (a Windows batch shim)
    # routes through cmd.exe, which interprets `|` as a pipe even
    # inside Python-quoted argv elements — gcloud receives a
    # fragmented filter, parse-errors out, and our try/except
    # silently returns []. The whole asset_changes source produced
    # zero evidence on every Windows demo box. iam_changes was
    # unaffected because its filter uses the OR keyword (no `|`
    # character).
    #
    # `:` is substring-contains, so it matches anywhere in the
    # method name (e.g., "v1.compute.firewalls.insert" contains
    # "insert"). The risk of false positives is low because GCP
    # audit-log method names are short and these verbs don't
    # commonly appear inside non-mutation method names.
    method_clauses = " OR ".join(
        f'protoPayload.methodName:"{suffix}"'
        for suffix in _CHANGE_METHOD_SUFFIXES
    )
    # Exclude IAM events — those go to gcp_iam_changes. We exclude by
    # serviceName (iam.googleapis.com) + by the SetIamPolicy method
    # (which fires across many services, not just iam.googleapis.com).
    filter_extra = (
        f'({method_clauses}) '
        f'AND NOT protoPayload.methodName="SetIamPolicy" '
        f'AND NOT protoPayload.serviceName="iam.googleapis.com"'
    )

    records = _log_client.query_audit_logs(
        project_id=project_id,
        start_iso=start_iso,
        end_iso=end_iso,
        filter_extra=filter_extra,
    )

    evidence: List[EvidenceItem] = []
    for idx, rec in enumerate(records):
        fields = _log_client.extract_audit_fields(rec)
        change_type = _classify_change_type(fields["method_name"])
        evidence.append(EvidenceItem(
            evidence_id=f"asset:{idx}",
            source="gcp_asset_changes",
            timestamp=fields["timestamp"],
            change_type=change_type,
            resource_ref=fields["resource_name"],
            actor=fields["principal_email"],
            summary=fields["request_summary"],
            related_refs=[],   # Day 3: dep-graph traversal for related resources
            relevance_score=0.0,  # correlator fills this in
            raw_payload=rec,
        ))

    _log.info(
        "asset_changes_collected",
        project_id=project_id,
        count=len(evidence),
        window=f"{start_iso} → {end_iso}",
    )
    return evidence


def _classify_change_type(method_name: str) -> str:
    """Map an API method name to one of CREATE / MODIFY / DELETE."""
    if not method_name:
        return "MODIFY"  # safe default
    lower = method_name.lower()
    for suffix, change in _METHOD_TO_CHANGE_TYPE:
        if lower.endswith("." + suffix.lower()):
            return change
    # Fallback for non-conformant method names.
    return "MODIFY"
