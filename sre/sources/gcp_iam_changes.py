"""IAM-change evidence source (Phase 8 Day 2).

What this source produces
-------------------------
Every Admin Activity audit log entry within the lookback window that
represents an IAM policy change. Covers:

  * ``SetIamPolicy`` on any resource (project, bucket, KMS key, etc.)
  * IAM API method calls: roles.create/update/delete,
    serviceAccounts.create/delete, serviceAccountKeys.create
  * Inherited / propagated bindings (folder, org level — but we keep
    the source scoped to project for Phase 0).

Why IAM gets its own source
---------------------------
Privilege-escalation incidents are a separate triage flow from
resource-CRUD incidents:

  * The blast radius is different — a misapplied IAM binding hits
    every resource under the policy, not just one.
  * The correlator weights IAM events higher when the alert's
    severity is SEV1/SEV2 (a hot security signal), lower for SEV3+.
  * Separate source chip in the UI tells the operator "the agent
    looked at IAM changes specifically" — important during audits.

The filter
----------
Anything where the methodName is SetIamPolicy OR the serviceName is
iam.googleapis.com. This catches both project-policy edits and
service-account / custom-role mutations.
"""

from __future__ import annotations

from typing import List

from common.logging import get_logger

from ..results import AlertEnvelope, EvidenceItem
from . import _log_client


_log = get_logger(__name__)


def collect(
    *,
    alert: AlertEnvelope,
    project_id: str,
    lookback_min: int,
) -> List[EvidenceItem]:
    """Collect IAM changes inside the alert's lookback window."""
    start_iso, end_iso = _log_client.compute_window(
        fired_at_iso=alert.fired_at, lookback_min=lookback_min,
    )

    # Match either:
    #   * Any SetIamPolicy call (fires across services — projects,
    #     buckets, kms, secrets, etc.)
    #   * Any call to the IAM API (roles/SAs/keys)
    filter_extra = (
        'protoPayload.methodName="SetIamPolicy" '
        'OR protoPayload.serviceName="iam.googleapis.com"'
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
        change_type = _classify_change_type(fields["method_name"], rec)
        evidence.append(EvidenceItem(
            evidence_id=f"iam:{idx}",
            source="gcp_iam_changes",
            timestamp=fields["timestamp"],
            change_type=change_type,
            resource_ref=fields["resource_name"],
            actor=fields["principal_email"],
            summary=fields["request_summary"],
            related_refs=_extract_related_principals(rec),
            relevance_score=0.0,
            raw_payload=rec,
        ))

    _log.info(
        "iam_changes_collected",
        project_id=project_id,
        count=len(evidence),
        window=f"{start_iso} → {end_iso}",
    )
    return evidence


def _classify_change_type(method_name: str, record: dict) -> str:
    """Map an IAM-touching method to GRANT / REVOKE / MODIFY.

    SetIamPolicy ALONE isn't enough — it's used for both grants and
    revokes (and for no-op rewrites). We dig into
    ``serviceData.policyDelta.bindingDeltas[].action`` which is "ADD"
    or "REMOVE". When mixed, the most-impactful action wins (REVOKE
    > GRANT > MODIFY) so the correlator weights it correctly.
    """
    proto = record.get("protoPayload") or {}
    service_data = proto.get("serviceData") or {}
    deltas = (service_data.get("policyDelta") or {}).get("bindingDeltas") or []

    if deltas:
        actions = {str(d.get("action", "")).upper() for d in deltas}
        if "REMOVE" in actions:
            return "REVOKE"
        if "ADD" in actions:
            return "GRANT"

    # Method-name fallbacks for non-SetIamPolicy IAM API calls.
    if not method_name:
        return "MODIFY"
    lower = method_name.lower()
    if "delete" in lower:
        return "REVOKE"
    if "create" in lower or "insert" in lower:
        return "GRANT"
    if "setiampolicy" in lower:
        # SetIamPolicy with no deltas usually means a no-op rewrite
        # (operator applied the same policy via Terraform). Classify
        # as MODIFY so the correlator down-ranks it vs real GRANT/REVOKE.
        return "MODIFY"
    return "MODIFY"


def _extract_related_principals(record: dict) -> List[str]:
    """Pull the member(s) granted/revoked into related_refs.

    The correlator's resource-overlap scoring matches against
    ``alert.resource_refs``, but for IAM the more meaningful overlap
    is "did this change affect the SA / user the alert resource uses?"
    Day 3's LLM step can use this — for now we just capture the
    principals on the EvidenceItem.
    """
    proto = record.get("protoPayload") or {}
    service_data = proto.get("serviceData") or {}
    deltas = (service_data.get("policyDelta") or {}).get("bindingDeltas") or []
    principals = []
    for d in deltas:
        member = d.get("member")
        if member and member not in principals:
            principals.append(str(member))
    return principals
