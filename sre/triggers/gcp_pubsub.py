"""GCP Cloud Monitoring → Pub/Sub pull-subscription client.

Why pull (not push)
-------------------
Cloud Monitoring's alert pipeline delivers via a Pub/Sub *notification
channel*. The downstream subscription can be either *push* (Google
HTTP-POSTs to a URL you own) or *pull* (your code calls
``subscription.pull``). We chose pull because:

  * **Streamlit isn't an HTTPS receiver.** A push subscription needs
    a stable public URL with HTTPS auth — fine for a Cloud Run
    service, awkward for Streamlit pages that double as the UI.
  * **Identical code path local + prod.** Local dev uses
    Application Default Credentials (``gcloud auth
    application-default login``); Cloud Run uses workload identity.
    No webhook receiver to mock.
  * **Backpressure is free.** If the UI is closed, messages just sit
    in the subscription until they expire (or a poller picks them
    up). With push, we'd need a dead-letter topic from day one.

The trade-off — push has lower latency end-to-end — doesn't matter
here. Cloud Monitoring takes 30-60s to fire an alert anyway; our
~3-5s poll cadence is dwarfed by that.

Module API
----------
Two public functions used by the orchestrator + the Streamlit page:

  * ``list_pending_alerts(subscription_id, max_messages, timeout_s)``
    → ``List[AlertEnvelope]``. Polls once, returns whatever is
    available. Each envelope carries the Pub/Sub ``message_id`` and
    ``ack_id`` so the caller can ack after triage.

  * ``ack(subscription_id, ack_ids)`` / ``nack(subscription_id,
    ack_ids)``. Decoupled from list_pending_alerts on purpose — the
    operator might want to nack ("I can't triage this right now")
    instead of ack-after-success. UI exposes both as buttons.

Phase-0 deliberately keeps this synchronous + single-shot. Phase 1
will swap to ``subscriber.StreamingPullFuture`` for sub-second
latency, but the current Streamlit polling model works fine on top
of one-shot pulls and is easier to reason about during the demo.

Auth chain (resolved by the google-cloud-pubsub client)
-------------------------------------------------------
  1. GOOGLE_APPLICATION_CREDENTIALS env var (service account JSON path)
  2. gcloud auth application-default login (dev workstations)
  3. Workload identity / metadata server (Cloud Run / GKE)

No code in this module touches credentials — the SDK figures it out.
"""

from __future__ import annotations

import base64
import json
from typing import List, Optional, Sequence

from common.errors import EngineError, PreflightError
from common.logging import get_logger

from ..results import AlertEnvelope


_log = get_logger(__name__)


# Default subscription name. Matches scripts/sre_setup_gcp.sh — change
# both at once or the puller and the setup script will disagree.
DEFAULT_SUBSCRIPTION_ID = "sre-agent-pull-subscription"

# Max messages per pull. Pub/Sub allows up to 1000 but Cloud Monitoring
# rarely bursts more than a handful at once; the UI also renders the
# queue card-by-card so 10 keeps the page snappy.
DEFAULT_MAX_MESSAGES = 10

# Pull deadline. Pub/Sub returns immediately if messages are available,
# otherwise waits up to ``timeout_s`` for one to arrive. We keep this
# short so the Streamlit page doesn't freeze on quiet periods.
DEFAULT_PULL_TIMEOUT_S = 3


class PubSubUnavailable(EngineError):
    """Raised when the Pub/Sub SDK can't be imported or the subscription
    can't be reached. The Streamlit page catches this and shows a
    "configure Pub/Sub" empty state instead of a stack trace."""

    user_hint = (
        "Cloud Pub/Sub is not reachable. Check that the "
        "google-cloud-pubsub library is installed and that the agent "
        "has pubsub.subscriber on the configured subscription."
    )

    def __init__(self, message: str, *, subscription: Optional[str] = None,
                 cause: Optional[Exception] = None) -> None:
        super().__init__(message, subscription=subscription,
                         cause=type(cause).__name__ if cause else None)
        self.subscription = subscription


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def list_pending_alerts(
    *,
    project_id: str,
    subscription_id: str = DEFAULT_SUBSCRIPTION_ID,
    max_messages: int = DEFAULT_MAX_MESSAGES,
    timeout_s: float = DEFAULT_PULL_TIMEOUT_S,
) -> List[AlertEnvelope]:
    """Pull pending alerts from the SRE agent's subscription.

    Single-shot: returns whatever is queued (up to ``max_messages``)
    and returns. Messages are NOT acked here — the caller acks after
    successful triage via :func:`ack`. Unacked messages get
    redelivered after the subscription's ack-deadline (300s in the
    setup script), which is fine for incidents where the operator
    might walk away from a half-finished triage.

    Args:
        project_id: GCP project that owns the subscription. The
            subscription's full resource name is built as
            ``projects/<project_id>/subscriptions/<subscription_id>``.
        subscription_id: short subscription name (default matches
            ``sre_setup_gcp.sh``).
        max_messages: cap on returned envelopes per call.
        timeout_s: how long the API blocks waiting for at least one
            message before returning empty.

    Returns:
        List of AlertEnvelope. Empty list means "queue was empty" —
        NOT an error.

    Raises:
        PreflightError: empty project_id / subscription_id (caller
            misconfigured the page).
        PubSubUnavailable: SDK missing or subscription unreachable.
            UI shows a remediation hint instead of a traceback.
    """
    if not project_id:
        raise PreflightError(
            "list_pending_alerts() requires project_id",
            stage="validate_pubsub",
            reason="missing_project_id",
        )
    if not subscription_id:
        raise PreflightError(
            "list_pending_alerts() requires subscription_id",
            stage="validate_pubsub",
            reason="missing_subscription_id",
        )

    SubscriberClient, PullRequest = _import_pubsub()

    client = SubscriberClient()
    sub_path = client.subscription_path(project_id, subscription_id)

    log = _log.bind(subscription=sub_path)
    log.debug("pubsub_pull_start", max_messages=max_messages)

    try:
        # Pull is synchronous; the SDK handles retries internally.
        # ``return_immediately=False`` lets the server hold the
        # connection open up to ``timeout`` waiting for messages,
        # avoiding tight-loop polling from the Streamlit page.
        response = client.pull(
            request=PullRequest(
                subscription=sub_path,
                max_messages=max_messages,
                return_immediately=False,
            ),
            timeout=timeout_s,
        )
    except Exception as e:  # noqa: BLE001 — broad on purpose
        # Common failures: subscription doesn't exist (NotFound), no
        # IAM (PermissionDenied), network blip (ServiceUnavailable).
        # All look the same to the UI: "Pub/Sub not reachable".
        raise PubSubUnavailable(
            f"pull from {sub_path} failed: {e}",
            subscription=sub_path,
            cause=e,
        ) from e

    envelopes: List[AlertEnvelope] = []
    for received in response.received_messages:
        try:
            env = _parse_pubsub_message(received)
            envelopes.append(env)
        except Exception as parse_err:  # noqa: BLE001
            # Bad payload (not valid Cloud Monitoring JSON, etc.).
            # We DO NOT ack — let it redeliver so a human/code can
            # investigate. Logged at WARNING so it's visible without
            # being scary.
            log.warning(
                "pubsub_message_parse_failed",
                message_id=received.message.message_id,
                error=str(parse_err),
            )

    log.info(
        "pubsub_pull_complete",
        returned_count=len(envelopes),
        raw_count=len(response.received_messages),
    )
    return envelopes


def ack(
    *,
    project_id: str,
    ack_ids: Sequence[str],
    subscription_id: str = DEFAULT_SUBSCRIPTION_ID,
) -> int:
    """Acknowledge processed messages. Returns count of ack_ids sent.

    Pub/Sub batches up to 2500 ack_ids per request; we expect <100 in
    practice (one operator triaging a handful of alerts) so a single
    call is fine.
    """
    if not ack_ids:
        return 0
    SubscriberClient, _ = _import_pubsub()
    client = SubscriberClient()
    sub_path = client.subscription_path(project_id, subscription_id)
    try:
        client.acknowledge(
            request={"subscription": sub_path, "ack_ids": list(ack_ids)},
            timeout=DEFAULT_PULL_TIMEOUT_S,
        )
    except Exception as e:  # noqa: BLE001
        raise PubSubUnavailable(
            f"ack on {sub_path} failed: {e}",
            subscription=sub_path,
            cause=e,
        ) from e
    _log.info("pubsub_ack", subscription=sub_path, count=len(ack_ids))
    return len(ack_ids)


def nack(
    *,
    project_id: str,
    ack_ids: Sequence[str],
    subscription_id: str = DEFAULT_SUBSCRIPTION_ID,
) -> int:
    """Negatively-ack messages so Pub/Sub redelivers immediately.

    Implemented as ``modify_ack_deadline(0)`` — Pub/Sub's officially
    blessed way to nack. Returns count of ack_ids sent. UI exposes
    this as a "Defer / I can't take this" button on the alert card.
    """
    if not ack_ids:
        return 0
    SubscriberClient, _ = _import_pubsub()
    client = SubscriberClient()
    sub_path = client.subscription_path(project_id, subscription_id)
    try:
        client.modify_ack_deadline(
            request={
                "subscription": sub_path,
                "ack_ids": list(ack_ids),
                "ack_deadline_seconds": 0,
            },
            timeout=DEFAULT_PULL_TIMEOUT_S,
        )
    except Exception as e:  # noqa: BLE001
        raise PubSubUnavailable(
            f"nack on {sub_path} failed: {e}",
            subscription=sub_path,
            cause=e,
        ) from e
    _log.info("pubsub_nack", subscription=sub_path, count=len(ack_ids))
    return len(ack_ids)


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _import_pubsub():
    """Lazy import so the module is importable in environments that
    don't have google-cloud-pubsub installed (CI, unit tests, the
    local-dev Streamlit smoke check). Returns the two SDK pieces we
    use as a tuple.

    Raises:
        PubSubUnavailable: SDK missing. UI shows the install hint.
    """
    try:
        from google.cloud.pubsub_v1 import SubscriberClient
        from google.cloud.pubsub_v1.types import PullRequest
    except ImportError as e:
        raise PubSubUnavailable(
            "google-cloud-pubsub is not installed; "
            "run `pip install google-cloud-pubsub`",
            cause=e,
        ) from e
    return SubscriberClient, PullRequest


def _parse_pubsub_message(received) -> AlertEnvelope:
    """Convert a ``ReceivedMessage`` protobuf into an AlertEnvelope.

    Cloud Monitoring posts a specific JSON shape into ``data``. Other
    Pub/Sub publishers (demo seeder, future trigger sources) post a
    superset of that same shape. We delegate the actual field mapping
    to :mod:`sre.triggers.alert_parser` (Day-1 sibling module) so the
    parsing logic stays in one place — this function is only
    responsible for the bytes → dict step and stamping the Pub/Sub
    bookkeeping fields onto the envelope.

    Args:
        received: ``google.pubsub_v1.types.ReceivedMessage``.

    Returns:
        AlertEnvelope with ``pubsub_message_id`` and ``pubsub_ack_id``
        populated. Parser fills the rest.

    Raises:
        ValueError: payload isn't valid UTF-8 JSON. Caller swallows
            + nack-by-omission (we don't ack, Pub/Sub redelivers).
    """
    msg = received.message
    raw_bytes = msg.data  # already bytes in the pubsub_v1 client
    if not raw_bytes:
        raise ValueError("empty Pub/Sub message body")

    # Cloud Monitoring sends UTF-8 JSON directly in `data`. Some test
    # publishers (and Cloud Logging sinks) base64-encode it first. We
    # try raw-JSON first and fall back to base64-then-JSON so both
    # paths "just work".
    try:
        payload = json.loads(raw_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        try:
            decoded = base64.b64decode(raw_bytes, validate=True)
            payload = json.loads(decoded.decode("utf-8"))
        except Exception as e:  # noqa: BLE001
            raise ValueError(f"could not decode Pub/Sub data as JSON: {e}") from e

    # Hand the dict to the dedicated parser. We import locally to keep
    # this module's top-level deps minimal and to avoid a circular
    # import if alert_parser ever wants to depend on results.
    from . import alert_parser

    envelope = alert_parser.normalize(
        payload=payload,
        attributes=dict(msg.attributes) if msg.attributes else {},
    )

    # Stamp Pub/Sub bookkeeping. The orchestrator never sees these
    # except via the envelope — alert_parser shouldn't know about
    # them (it might run on payloads from non-Pub/Sub sources later).
    envelope.pubsub_message_id = msg.message_id
    envelope.pubsub_ack_id = received.ack_id
    return envelope
