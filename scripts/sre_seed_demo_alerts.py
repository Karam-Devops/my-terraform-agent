#!/usr/bin/env python3
"""Publish canned demo alerts to the SRE agent's Pub/Sub topic.

Why this exists
---------------
The SRE Agent's Phase-0 ingestion path is Cloud Monitoring →
Notification Channel → Pub/Sub → pull subscription. In production
that pipeline triggers from real metric anomalies. For the demo
(and for local iteration when no alerts are firing), this script
publishes hand-crafted messages to the SAME topic the production
notification channel writes to. The agent can't tell the two apart
— same code path, same parsing, same result.

Three default scenarios, each calibrated to a common high-value
triage flow:

  1. **SEV2 — ALB 5xx > 5%.** The "edge/ingress regressed" pattern.
     Lookback finds an SG change + a deploy.
  2. **SEV1 — Cloud SQL connections > 90%.** The "DB hot under load"
     pattern. Lookback finds an IAM grant + a connection pool
     config change.
  3. **SEV2 — GKE Pod ImagePullBackOff.** The "rollout broke" pattern.
     Lookback finds a deploy with a misspelled image tag.

Usage
-----
  python scripts/sre_seed_demo_alerts.py --project=dev-proj-470211
  python scripts/sre_seed_demo_alerts.py --project=dev-proj-470211 \\
      --scenario=alb_5xx
  python scripts/sre_seed_demo_alerts.py --project=dev-proj-470211 \\
      --topic=sre-incident-alerts --all

The script uses the demo-seeder JSON shape (flat top-level) which
``sre.triggers.alert_parser.normalize()`` auto-detects via the
``alert_id`` key — same parser path used by real Cloud Monitoring
payloads, just a different branch.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import uuid
from typing import Dict, List


# ---------------------------------------------------------------------------
# Demo scenarios. Each entry is a dict with the demo-seeder shape that
# alert_parser._from_demo_seeder() handles. Keep these realistic — the
# UI screenshot quality + the believability of the demo depend on it.
# ---------------------------------------------------------------------------


def _build_scenarios(project_id: str) -> Dict[str, Dict]:
    """Return scenario_name → payload dict, parameterized by project."""
    now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    return {
        "alb_5xx": {
            "alert_id":    f"demo-alb-{uuid.uuid4().hex[:8]}",
            "source":      "gcp_cloud_monitoring",
            "policy_name": "ALB 5xx Error Rate > 5%",
            "summary": (
                "5xx error rate on payments-prod-alb has exceeded the "
                "warning threshold (5%). p95 latency stable. No upstream "
                "errors reported in the last 5 minutes."
            ),
            "severity":    "SEV2",
            "project_id":  project_id,
            "resource_refs": [
                f"projects/{project_id}/instances/payments-prod-alb",
                f"projects/{project_id}/firewalls/payments-prod-sg",
            ],
            "fired_at":    now_iso,
            "labels": {
                "team":         "payments",
                "env":          "prod",
                "console_url":  "https://console.cloud.google.com/monitoring",
            },
        },
        "cloudsql_conns": {
            "alert_id":    f"demo-sql-{uuid.uuid4().hex[:8]}",
            "source":      "gcp_cloud_monitoring",
            "policy_name": "Cloud SQL connection utilization > 90%",
            "summary": (
                "orders-db is at 91% of max connections (455/500). "
                "Connection age distribution shifted toward older "
                "connections in the last 15 minutes."
            ),
            "severity":    "SEV1",
            "project_id":  project_id,
            "resource_refs": [
                f"projects/{project_id}/instances/orders-db",
            ],
            "fired_at":    now_iso,
            "labels": {
                "team":         "orders",
                "env":          "prod",
                "metric_type":  "cloudsql.googleapis.com/database/network/connections",
                "console_url":  "https://console.cloud.google.com/sql/instances",
            },
        },
        "gke_imagepull": {
            "alert_id":    f"demo-gke-{uuid.uuid4().hex[:8]}",
            "source":      "gcp_cloud_monitoring",
            "policy_name": "GKE Pod ImagePullBackOff",
            "summary": (
                "payments-api deployment has 3/3 pods in "
                "ImagePullBackOff for the last 4 minutes. Last deploy: "
                "12 minutes ago."
            ),
            "severity":    "SEV2",
            "project_id":  project_id,
            "resource_refs": [
                f"projects/{project_id}/clusters/payments-cluster/pods/payments-api",
            ],
            "fired_at":    now_iso,
            "labels": {
                "team":         "payments",
                "env":          "prod",
                "resource_type": "k8s_pod",
                "console_url":  "https://console.cloud.google.com/kubernetes",
            },
        },
    }


# ---------------------------------------------------------------------------
# Pub/Sub publishing
# ---------------------------------------------------------------------------


def _publish(project_id: str, topic_name: str, payload: Dict) -> str:
    """Publish one payload as JSON bytes to the topic. Returns message_id.

    Imports lazily so the script's --list / --help flags work without
    google-cloud-pubsub installed (useful when the operator just wants
    to see the scenarios without setting up the SDK first).
    """
    try:
        from google.cloud import pubsub_v1
    except ImportError:
        print(
            "ERROR: google-cloud-pubsub is not installed.\n"
            "       Install with:  pip install google-cloud-pubsub",
            file=sys.stderr,
        )
        sys.exit(2)

    publisher = pubsub_v1.PublisherClient()
    topic_path = publisher.topic_path(project_id, topic_name)
    body = json.dumps(payload).encode("utf-8")
    # Attach the alert_id as a Pub/Sub attribute so the operator can
    # filter on it in Cloud Logging without paying for body parsing.
    attrs = {
        "alert_id": payload["alert_id"],
        "severity": payload.get("severity", "SEV3"),
        "source":   "demo_seeder",
    }
    future = publisher.publish(topic_path, body, **attrs)
    # .result() blocks until the publish acks. Demo seeder is one-shot
    # so blocking is fine; the SDK retries internally on transient errors.
    message_id = future.result(timeout=30)
    return message_id


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: List[str]) -> int:
    parser = argparse.ArgumentParser(
        description="Publish canned SRE-agent demo alerts to a Pub/Sub topic.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--project", required=True,
        help="GCP project ID hosting the topic.",
    )
    parser.add_argument(
        "--topic", default="sre-incident-alerts",
        help="Pub/Sub topic name (must match scripts/sre_setup_gcp.sh).",
    )
    parser.add_argument(
        "--scenario",
        choices=("alb_5xx", "cloudsql_conns", "gke_imagepull"),
        help="Publish exactly one scenario. Omit + use --all for all three.",
    )
    parser.add_argument(
        "--all", action="store_true",
        help="Publish all three scenarios in sequence.",
    )
    parser.add_argument(
        "--list", action="store_true",
        help="Print available scenarios + their payloads as JSON and exit.",
    )
    args = parser.parse_args(argv)

    scenarios = _build_scenarios(args.project)

    if args.list:
        print(json.dumps(scenarios, indent=2))
        return 0

    if not args.scenario and not args.all:
        # Default = SEV2 ALB case. It's the most demonstrable: covers
        # the "what changed in the last hour" use case the user called
        # out as 60% of customer value.
        args.scenario = "alb_5xx"

    if args.all:
        targets = list(scenarios.keys())
    else:
        targets = [args.scenario]

    rc = 0
    for name in targets:
        payload = scenarios[name]
        try:
            msg_id = _publish(args.project, args.topic, payload)
        except Exception as e:  # noqa: BLE001
            print(f"FAIL  {name}: {e}", file=sys.stderr)
            rc = 1
            continue
        print(
            f"OK    {name:<16}  alert_id={payload['alert_id']}  "
            f"severity={payload['severity']}  message_id={msg_id}"
        )
    return rc


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
