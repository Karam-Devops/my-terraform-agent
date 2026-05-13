"""Normalize incoming alert payloads into AlertEnvelope.

One module, one job: turn whatever JSON arrived on the trigger (today
Cloud Monitoring; tomorrow PagerDuty, Datadog, Opsgenie) into the
provider-agnostic ``AlertEnvelope`` the rest of the engine consumes.

Why a dedicated parser
----------------------
Each trigger source emits a different JSON shape — and Cloud
Monitoring alone has two shapes (legacy v1 + the newer v3 "incident"
payload). Pushing the field-mapping into the puller module would
couple the transport (Pub/Sub) to the schema (Cloud Monitoring). When
we add PagerDuty webhooks in Phase 4, we'd be patching the puller
just to add a new payload variant. Keeping parsing here means the
puller is a dumb byte-pipe and the parser owns the schema knowledge.

Supported payload shapes (Phase 0)
----------------------------------
1. **Cloud Monitoring "v3 incident" JSON.** What the GCP Pub/Sub
   notification channel actually emits. Shape::

       {
         "version": "1.2",
         "incident": {
           "incident_id": "...",
           "scoping_project_id": "dev-proj-470211",
           "started_at": 1715520000,
           "ended_at": null,
           "policy_name": "...",
           "condition_name": "...",
           "url": "https://console.cloud.google.com/...",
           "state": "OPEN",
           "summary": "...",
           "severity": "ERROR" | "WARNING" | "CRITICAL" | "INFO",
           "resource": {"type": "...", "labels": {...}},
           "resource_name": "...",
           "metric": {"type": "..."},
           "policy_user_labels": {...},
           ...
         }
       }

2. **mtagent demo-seeder JSON.** A flat shape the seed script uses
   so we don't have to hand-write the Cloud Monitoring envelope just
   to run the demo. Shape::

       {
         "alert_id":      "demo-001",
         "policy_name":   "ALB 5xx > 5%",
         "summary":       "5xx error rate spiked ...",
         "severity":      "SEV2",
         "project_id":    "dev-proj-470211",
         "resource_refs": ["..."],
         "fired_at":      "2026-05-13T10:00:00Z",
         "labels":        {...}
       }

The parser auto-detects which shape it's looking at by presence of
the ``incident`` key (Cloud Monitoring) vs ``alert_id`` (demo).
Adding a new trigger source = adding a new branch + a new helper.
"""

from __future__ import annotations

import datetime
from typing import Any, Dict, Mapping, Optional

from ..results import (
    AlertEnvelope,
    SEV1, SEV2, SEV3, SEV4, SEV_INFO,
)


# Cloud Monitoring's severity vocabulary → our SEV1..4/INFO buckets.
# Mapping comes from the GCP docs and the PagerDuty integration guide
# (which uses the same mapping in reverse). Conservative: anything
# unknown lands at SEV3 so it's neither ignored nor wakes up a pager.
_CM_SEVERITY_MAP = {
    "CRITICAL": SEV1,
    "ERROR":    SEV2,
    "WARNING":  SEV3,
    "NOTICE":   SEV4,
    "INFO":     SEV_INFO,
    "DEBUG":    SEV_INFO,
    # Already-normalized values are accepted too — makes the demo
    # seeder + replayed audit blobs round-trip cleanly.
    SEV1: SEV1, SEV2: SEV2, SEV3: SEV3, SEV4: SEV4, SEV_INFO: SEV_INFO,
}


def normalize(
    *,
    payload: Mapping[str, Any],
    attributes: Optional[Mapping[str, str]] = None,
) -> AlertEnvelope:
    """Convert one decoded alert payload to an AlertEnvelope.

    Args:
        payload: The decoded JSON dict from Pub/Sub.data.
        attributes: Pub/Sub message attributes (the small string-only
            key/value map publishers can attach). Cloud Monitoring
            includes ``incident_id`` and ``state`` here; we copy them
            into the envelope's ``labels`` so downstream code can
            filter without re-parsing.

    Returns:
        AlertEnvelope. ``pubsub_message_id`` / ``pubsub_ack_id`` are
        NOT set here — the puller stamps those after we return.

    Raises:
        ValueError: payload doesn't match any known shape. Caller
            (puller) logs + skips ack so Pub/Sub redelivers.
    """
    attrs: Dict[str, str] = dict(attributes or {})

    if "incident" in payload and isinstance(payload["incident"], Mapping):
        return _from_cloud_monitoring(payload, attrs)

    if "alert_id" in payload:
        return _from_demo_seeder(payload, attrs)

    # Future: PagerDuty's `event` key, Datadog's `alert_type`, etc.
    # land as additional branches here.
    raise ValueError(
        "unrecognized alert payload shape — expected 'incident' "
        "(Cloud Monitoring) or 'alert_id' (demo) at the top level; "
        f"got keys: {sorted(payload.keys())}"
    )


# ---------------------------------------------------------------------------
# Shape-specific parsers
# ---------------------------------------------------------------------------


def _from_cloud_monitoring(
    payload: Mapping[str, Any],
    attrs: Dict[str, str],
) -> AlertEnvelope:
    """Cloud Monitoring v3 'incident' payload → AlertEnvelope."""
    incident: Mapping[str, Any] = payload["incident"]

    # ID. Cloud Monitoring's incident_id is globally unique. Fall
    # back to the Pub/Sub attributes copy if the body somehow drops
    # it (defensive; shouldn't happen in practice).
    alert_id = (
        incident.get("incident_id")
        or attrs.get("incident_id")
        or _synthetic_id(incident)
    )

    # Timestamp. Cloud Monitoring sends `started_at` as a Unix epoch
    # int. We store ISO-8601 UTC throughout the platform so logs +
    # snapshots + the UI all speak one timezone.
    fired_at = _epoch_to_iso(incident.get("started_at")) or _utc_iso_now()

    # Severity. Cloud Monitoring uses CRITICAL/ERROR/WARNING/...
    # Translate to SEV1..4. Operators rarely set per-policy severity
    # so most incidents land as SEV2/SEV3 by default — that's fine,
    # the correlator + LLM still rank them correctly.
    severity = _CM_SEVERITY_MAP.get(
        (incident.get("severity") or "").upper(),
        SEV3,
    )

    # Resource refs. Cloud Monitoring's `resource_name` is the
    # canonical pointer; `resource.labels` carries project/instance/
    # zone/etc. We promote both into the envelope so the correlator's
    # resource-overlap scoring sees as many candidate matches as
    # possible.
    resource_refs = []
    rn = incident.get("resource_name")
    if rn:
        resource_refs.append(rn)
    resource_block = incident.get("resource") or {}
    for label_val in (resource_block.get("labels") or {}).values():
        if label_val and label_val not in resource_refs:
            resource_refs.append(label_val)

    # Labels. We flatten everything the operator might filter on into
    # one dict — Cloud Monitoring policy user labels + the resource
    # type + the metric type + a couple of Pub/Sub attributes. Keep
    # values as strings (envelope.labels is Dict[str, str]).
    labels: Dict[str, str] = {}
    for k, v in (incident.get("policy_user_labels") or {}).items():
        labels[str(k)] = str(v)
    if "type" in resource_block:
        labels["resource_type"] = str(resource_block["type"])
    metric_block = incident.get("metric") or {}
    if "type" in metric_block:
        labels["metric_type"] = str(metric_block["type"])
    if attrs.get("state"):
        labels["state"] = attrs["state"]
    # Console URL — extremely useful for the operator (one click to
    # the alert page in GCP Console). Carried as a label since the
    # envelope schema doesn't have a first-class URL field.
    if incident.get("url"):
        labels["console_url"] = str(incident["url"])

    project_id = (
        incident.get("scoping_project_id")
        or incident.get("project_id")
        or (resource_block.get("labels") or {}).get("project_id")
    )

    return AlertEnvelope(
        alert_id=str(alert_id),
        source="gcp_cloud_monitoring",
        fired_at=fired_at,
        policy_name=str(
            incident.get("policy_name")
            or incident.get("condition_name")
            or "Cloud Monitoring alert"
        ),
        summary=str(incident.get("summary") or "(no summary)"),
        severity=severity,
        resource_refs=resource_refs,
        project_id=project_id,
        labels=labels,
        raw_payload=dict(payload),
    )


def _from_demo_seeder(
    payload: Mapping[str, Any],
    attrs: Dict[str, str],
) -> AlertEnvelope:
    """Flat demo-seeder JSON → AlertEnvelope.

    This shape is intentionally close to AlertEnvelope's own fields so
    the seeder script can be a thin JSON template. We still validate
    + coerce defensively because the seeder is hand-edited.
    """
    severity_in = str(payload.get("severity") or SEV3).upper()
    severity = _CM_SEVERITY_MAP.get(severity_in, severity_in)
    if severity not in (SEV1, SEV2, SEV3, SEV4, SEV_INFO):
        severity = SEV3

    raw_refs = payload.get("resource_refs") or []
    if isinstance(raw_refs, str):
        # Tolerate "a,b,c" as a convenience.
        raw_refs = [r.strip() for r in raw_refs.split(",") if r.strip()]

    labels = {str(k): str(v) for k, v in (payload.get("labels") or {}).items()}
    if attrs:
        # Promote attrs so the demo can carry routing hints without
        # bloating the body.
        for k, v in attrs.items():
            labels.setdefault(f"attr.{k}", str(v))

    return AlertEnvelope(
        alert_id=str(payload["alert_id"]),
        source=str(payload.get("source") or "demo_seeder"),
        fired_at=str(payload.get("fired_at") or _utc_iso_now()),
        policy_name=str(payload.get("policy_name") or "demo alert"),
        summary=str(payload.get("summary") or ""),
        severity=severity,
        resource_refs=[str(r) for r in raw_refs],
        project_id=payload.get("project_id"),
        labels=labels,
        raw_payload=dict(payload),
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _epoch_to_iso(value: Any) -> Optional[str]:
    """Cloud Monitoring sends Unix epoch seconds. Tolerate millis +
    string-encoded numbers too. Returns None for missing / invalid
    values so the caller can substitute 'now'."""
    if value is None or value == "":
        return None
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    # Heuristic: anything > 10^12 is millis (year ~33658 in seconds).
    if v > 1e12:
        v = v / 1000.0
    try:
        return datetime.datetime.fromtimestamp(
            v, tz=datetime.timezone.utc,
        ).isoformat(timespec="seconds")
    except (OverflowError, OSError, ValueError):
        return None


def _utc_iso_now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat(
        timespec="seconds"
    )


def _synthetic_id(incident: Mapping[str, Any]) -> str:
    """Last-resort alert ID when Cloud Monitoring's incident_id is
    missing. Stable hash of policy_name + started_at so the same
    payload always gets the same ID (idempotent ingest)."""
    import hashlib
    key = f"{incident.get('policy_name','?')}|{incident.get('started_at','?')}"
    return "synth-" + hashlib.sha1(key.encode("utf-8")).hexdigest()[:12]
