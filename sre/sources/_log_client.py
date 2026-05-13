"""Shared Cloud Logging audit-log query helper.

Why subprocess and not the SDK
------------------------------
The platform already wraps ``gcloud`` calls via ``importer.shell_runner``
with structured logging + per-call timeouts + typed ``UpstreamTimeout``.
Adding the ``google-cloud-logging`` SDK as a new dep would duplicate that
machinery (the SDK has its own auth chain, its own timeout semantics, and
no integration with our structured logs). The Dockerfile already carries
gcloud and audit-log queries are read-only — subprocess is the right
tool here.

What this module gives the source collectors
--------------------------------------------
A single function, :func:`query_audit_logs`, that hides the gcloud command
shape + JSON parsing + time-window formatting. Each source collector
(``gcp_asset_changes``, ``gcp_iam_changes``) builds its own ``filter_extra``
clause expressing the methodName / serviceName subset it cares about; the
helper handles everything else:

  * computes the ISO-8601 time window from ``alert.fired_at`` and
    ``lookback_min``
  * appends the standard ``logName=...%2Factivity`` selector (Admin
    Activity is the always-on, free log channel where CRUD + IAM
    changes land)
  * orders newest-first (operators read top-down)
  * caps result count (audit logs in a busy project can run into the
    thousands inside an hour; we sample the most recent)

Returns a list of decoded log dicts. Each dict is the full audit log
record — caller picks fields. Helper :func:`extract_audit_fields`
pulls the most commonly-used fields (timestamp, method, principal,
resource) into a flat shape suitable for ``EvidenceItem``.
"""

from __future__ import annotations

import datetime
import json
import os
import subprocess
import sys
from typing import Any, Dict, List, Optional

from common.logging import get_logger
from importer.shell_runner import run_command


class AuditLogQueryError(Exception):
    """Raised when the gcloud logging-read call exits non-zero.

    Carries the stderr snippet so the orchestrator can surface it on
    the source chip — operators see the actual error (filter parse
    failure, missing IAM, API not enabled) instead of a misleading
    "OK · 0 items" status.

    Day-4d learning: silently returning [] on subprocess errors hid
    a real filter-parse bug for an entire debug session. Make the
    failure visible by default — callers can still catch + treat as
    empty if they want lenient behavior.
    """

    def __init__(self, message: str, *, returncode: int, stderr: str) -> None:
        super().__init__(message)
        self.returncode = returncode
        self.stderr = stderr


# Windows gotcha: gcloud ships as gcloud.cmd (batch script), not
# gcloud.exe. subprocess.Popen with a list argv looks for an .exe and
# fails with [WinError 2] on Windows even though `gcloud ...` works
# fine at the shell prompt (cmd.exe + PATHEXT resolve the .cmd
# automatically). We disambiguate at the Python layer once here and
# reuse the constant in all subprocess calls.
_GCLOUD_BIN = "gcloud.cmd" if sys.platform == "win32" else "gcloud"

# Post-buffer for the evidence lookback window. The Day-1 default of 5
# min was too tight for two real-world scenarios:
#   (a) Cloud Monitoring's alert evaluation can lag 1-2 min behind the
#       triggering metric anomaly — a 5-min buffer barely covers it.
#   (b) Demo flows that seed an alert THEN generate evidence post-seed:
#       the test event lands after fired_at + 5min and falls outside
#       the window, even though it's clearly the operator's intent
#       to investigate it.
# 30 min is the sweet spot — still tight enough to avoid drowning the
# correlator in unrelated activity, broad enough to cover both cases.
# Operators tuning per-tenant can override via the env var.
_DEFAULT_POST_BUFFER_MIN = int(os.environ.get("SRE_POST_BUFFER_MIN", "30"))


_log = get_logger(__name__)


# Admin Activity log channel — always on, free, captures CRUD + IAM
# changes. Data Access logs are off by default and would require
# customer opt-in. Phase 8 deliberately stays in Admin Activity so
# onboarding stays at zero customer config.
_ADMIN_ACTIVITY_LOGNAME_TMPL = (
    "projects/{project_id}/logs/cloudaudit.googleapis.com%2Factivity"
)

# Max audit-log records pulled per source per triage. Tuned to:
#   * a busy prod project produces ~hundreds of admin events per hour,
#     so the natural cap is 300-500 to avoid blowing up the correlator
#     while still covering realistic burst rates.
#   * Claude's context window comfortably handles ~200 evidence items
#     with full reasoning; past that we start losing fidelity in the
#     hypothesis writeup (Day 3).
DEFAULT_MAX_RECORDS = 250

# Per-call gcloud timeout. Logging queries on a quiet project return
# in ~1s; a wide filter on a busy project can take 10-15s. 45s gives
# headroom without hanging the triage UI.
DEFAULT_TIMEOUT_S = 45.0


def query_audit_logs(
    *,
    project_id: str,
    start_iso: str,
    end_iso: str,
    filter_extra: str = "",
    max_records: int = DEFAULT_MAX_RECORDS,
    timeout_s: float = DEFAULT_TIMEOUT_S,
) -> List[Dict[str, Any]]:
    """Query Cloud Logging admin-activity logs for the given time window.

    Args:
        project_id: GCP project to query. Single-project for Phase 0;
            cross-project (folder-level audit views) lands in Phase 1.
        start_iso: lower bound on ``timestamp`` (inclusive). ISO-8601 UTC.
        end_iso: upper bound on ``timestamp`` (inclusive). ISO-8601 UTC.
        filter_extra: additional gcloud-logging filter clause AND-ed
            onto the time + logName base. Examples::

                'protoPayload.methodName="SetIamPolicy"'
                'protoPayload.serviceName="compute.googleapis.com"'

            The caller is responsible for quote-safety — values that
            contain spaces / quotes must be pre-quoted (gcloud's
            filter language uses double-quotes for string values).
        max_records: cap on returned records. Newest-first ordering
            means we keep the most-recent N when truncated.
        timeout_s: per-call wall-clock budget. Surfaces as
            ``UpstreamTimeout`` if exceeded — orchestrator records
            the source as ``status="failed"`` and continues.

    Returns:
        List of decoded audit-log records (dicts). Empty list if the
        query found nothing OR if gcloud returned a non-fatal error
        (e.g., logging API not enabled on the project — that's a
        configuration issue we surface via the source-status chip,
        not a triage-blocking error).

    Raises:
        UpstreamTimeout: the gcloud call exceeded ``timeout_s``.
    """
    log_name = _ADMIN_ACTIVITY_LOGNAME_TMPL.format(project_id=project_id)

    # Compose the full filter. Order matters for gcloud's optimizer —
    # logName + timestamp first (indexed), free-text predicates after.
    filter_parts = [
        f'logName="{log_name}"',
        f'timestamp>="{start_iso}"',
        f'timestamp<="{end_iso}"',
    ]
    if filter_extra.strip():
        filter_parts.append(f"({filter_extra})")
    filter_str = " AND ".join(filter_parts)

    cmd = [
        _GCLOUD_BIN, "logging", "read", filter_str,
        f"--project={project_id}",
        f"--limit={max_records}",
        "--order=desc",          # newest-first
        "--format=json",
    ]

    _log.info(
        "audit_log_query_start",
        project_id=project_id,
        start=start_iso,
        end=end_iso,
        filter_extra=filter_extra,
        max_records=max_records,
    )

    try:
        stdout = run_command(cmd, timeout=timeout_s)
    except subprocess.CalledProcessError as e:
        # Common causes that warrant an explicit FAILED chip:
        #   * Logging API not enabled — operator hasn't run setup yet
        #   * SA missing roles/logging.viewer
        #   * Project ID typo
        #   * Filter parse error (Day-4e: this was hidden as "OK 0"
        #     for a full debug session before we promoted it to FAILED)
        stderr_snip = (e.stderr or "")[:500] if hasattr(e, "stderr") else ""
        _log.warning(
            "audit_log_query_failed",
            project_id=project_id,
            returncode=e.returncode,
            stderr=stderr_snip,
        )
        raise AuditLogQueryError(
            f"gcloud logging read exit {e.returncode}: {stderr_snip[:200]}",
            returncode=e.returncode,
            stderr=stderr_snip,
        ) from e

    if not stdout or not stdout.strip():
        return []

    try:
        records = json.loads(stdout)
    except json.JSONDecodeError as je:
        _log.warning(
            "audit_log_json_decode_failed",
            project_id=project_id,
            error=str(je),
            sample=stdout[:200],
        )
        return []

    # gcloud returns either a list or — when empty — the literal string
    # "[]" but sometimes the bytes pipeline yields an empty array as
    # null. Normalize.
    if not isinstance(records, list):
        return []

    _log.info(
        "audit_log_query_complete",
        project_id=project_id,
        returned_count=len(records),
    )
    return records


def compute_window(
    *, fired_at_iso: str, lookback_min: int,
    post_buffer_min: int = _DEFAULT_POST_BUFFER_MIN,
) -> tuple[str, str]:
    """Derive (start_iso, end_iso) for an alert + lookback.

    The window is ``[fired_at - lookback_min, fired_at + post_buffer_min]``.
    The post-buffer matters because alert fire times often lag the
    actual triggering event by 30-90s (Cloud Monitoring evaluation
    window) — and clock skew between log producers + the alert
    pipeline can drift another minute or two. The default 30 min
    accommodates both that lag AND the common demo flow where the
    operator generates test evidence after seeding the alert; see
    the SRE_POST_BUFFER_MIN env-var note at the top of this module.

    Args:
        fired_at_iso: alert.fired_at (ISO-8601 UTC).
        lookback_min: lookback window in minutes (validated by the
            orchestrator to be in [5, 360]).
        post_buffer_min: forward buffer in minutes.

    Returns:
        (start_iso, end_iso) — both ISO-8601 UTC strings ready to
        slot into a gcloud logging filter.
    """
    # ``fromisoformat`` accepts the ``+00:00`` and the ``Z`` suffix in
    # Python 3.11+, but to stay portable we strip ``Z`` and force UTC.
    cleaned = fired_at_iso.rstrip("Z")
    try:
        fired_dt = datetime.datetime.fromisoformat(cleaned)
    except ValueError:
        # Caller gave us a non-ISO string. Fall back to "now" so the
        # query still produces something — better than crashing.
        _log.warning(
            "audit_log_window_bad_fired_at",
            fired_at=fired_at_iso,
            reason="non-ISO; using now as anchor",
        )
        fired_dt = datetime.datetime.now(datetime.timezone.utc)

    if fired_dt.tzinfo is None:
        fired_dt = fired_dt.replace(tzinfo=datetime.timezone.utc)

    start_dt = fired_dt - datetime.timedelta(minutes=lookback_min)
    end_dt = fired_dt + datetime.timedelta(minutes=post_buffer_min)

    return (
        start_dt.isoformat(timespec="seconds"),
        end_dt.isoformat(timespec="seconds"),
    )


def extract_audit_fields(record: Dict[str, Any]) -> Dict[str, Any]:
    """Pull the audit-log fields we care about into a flat dict.

    Cloud Logging records are deeply nested protobuf-shaped JSON.
    Pulling the canonical fields into a flat shape once here keeps
    each source collector readable + makes it obvious where each
    EvidenceItem field comes from.

    Fields returned:
        timestamp:       ISO-8601 UTC (from the log entry's ``timestamp``)
        method_name:     e.g. "v1.compute.instances.delete"
        service_name:    e.g. "compute.googleapis.com"
        resource_name:   canonical resource path (when present)
        principal_email: actor (SA email or human email)
        principal_type:  "user" / "serviceAccount" / "system" (best-effort)
        request_summary: short human-readable; pulled from request blob
        resource_labels: dict (project_id, location, etc.)
        insert_id:       Cloud Logging's globally-unique record id
                         — handy as ``evidence_id`` prefix material

    Returns a dict with sensible defaults (empty strings, not None,
    so f-string interpolation in summary builders doesn't print
    "None").
    """
    proto = record.get("protoPayload") or {}
    auth = proto.get("authenticationInfo") or {}
    resource = record.get("resource") or {}

    principal = str(auth.get("principalEmail") or "")
    principal_type = _classify_principal(principal)

    method_name = str(proto.get("methodName") or "")
    service_name = str(proto.get("serviceName") or "")

    # Resource name lives in several places depending on the API:
    #   * protoPayload.resourceName (most APIs)
    #   * resource.labels.* (always present, less specific)
    resource_name = (
        str(proto.get("resourceName") or "")
        or _build_resource_path(resource)
    )

    return {
        "timestamp":       str(record.get("timestamp") or ""),
        "method_name":     method_name,
        "service_name":    service_name,
        "resource_name":   resource_name,
        "principal_email": principal,
        "principal_type":  principal_type,
        "request_summary": _summarize_request(proto),
        "resource_labels": dict(resource.get("labels") or {}),
        "insert_id":       str(record.get("insertId") or ""),
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _classify_principal(email: str) -> str:
    """Heuristic principal classification.

    Why: the correlator weights human-driven changes higher than
    machine-driven ones (humans break things more often than
    well-tested CI pipelines). We don't have an authoritative
    "is this a human?" signal, but Google's SA emails follow a
    strict pattern.
    """
    if not email:
        return "system"
    if email.endswith(".gserviceaccount.com") or email.startswith("service-"):
        return "serviceAccount"
    if email == "google-managed-services":
        return "system"
    return "user"


def _build_resource_path(resource: Dict[str, Any]) -> str:
    """Best-effort canonical path from resource.type + resource.labels.

    Cloud Logging's ``resource.type`` + ``resource.labels`` is always
    present, but the natural-language path varies by service. This
    helper produces something the correlator's resource-overlap
    scoring can match against ``alert.resource_refs``.
    """
    rtype = str(resource.get("type") or "")
    labels = resource.get("labels") or {}
    project = labels.get("project_id", "")
    if not rtype:
        return ""

    # A handful of known types — adding more is purely additive. For
    # unknown types we fall through to a generic "type:name" form so
    # the correlator still has something to substring-match on.
    if rtype == "gce_instance":
        instance = labels.get("instance_id", "") or labels.get("instance_name", "")
        zone = labels.get("zone", "")
        return f"projects/{project}/zones/{zone}/instances/{instance}".rstrip("/")
    if rtype == "cloud_run_revision":
        rev = labels.get("revision_name", "")
        loc = labels.get("location", "")
        svc = labels.get("service_name", "")
        return f"projects/{project}/locations/{loc}/services/{svc}/revisions/{rev}".rstrip("/")
    if rtype == "k8s_cluster":
        loc = labels.get("location", "")
        name = labels.get("cluster_name", "")
        return f"projects/{project}/locations/{loc}/clusters/{name}".rstrip("/")
    if rtype == "gcs_bucket":
        return f"buckets/{labels.get('bucket_name', '')}"
    if rtype == "cloudsql_database":
        return f"projects/{project}/instances/{labels.get('database_id', '')}"
    if rtype == "iam_role":
        return f"projects/{project}/roles/{labels.get('role_name', '')}"

    # Fallback. Keep it stable so the correlator can still substring-match.
    most_specific = (
        labels.get("name")
        or labels.get("resource_name")
        or labels.get("instance_id")
        or ""
    )
    if most_specific:
        return f"{rtype}:{most_specific}"
    return rtype


def _summarize_request(proto: Dict[str, Any]) -> str:
    """One-line human summary of a protoPayload.

    The full request body is too verbose for an EvidenceItem.summary
    (often 50+ fields). We pull the most discriminating piece —
    method name + a target identifier — into a short string.

    For SetIamPolicy: extract the bindings count / member changes.
    For *.insert / *.update / *.delete: extract the target resource.

    Examples:
        "compute.instances.delete on payments-prod-alb"
        "SetIamPolicy granted roles/owner to alice@example.com"
    """
    method = str(proto.get("methodName") or "")
    resource_short = str(proto.get("resourceName") or "").rsplit("/", 1)[-1]

    # IAM policy changes carry a serviceData.policyDelta with the
    # specific bindings touched — most useful summary for incidents.
    service_data = proto.get("serviceData") or {}
    policy_delta = (service_data.get("policyDelta") or {})
    if policy_delta:
        binding_deltas = policy_delta.get("bindingDeltas") or []
        if binding_deltas:
            first = binding_deltas[0]
            action = first.get("action", "?")
            role = first.get("role", "?")
            member = first.get("member", "?")
            return f"{method} {action} {role} on {member}"

    if method and resource_short:
        return f"{method} on {resource_short}"
    return method or "audit event"
