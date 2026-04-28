# importer/_describe_router.py
"""Per-service describe handlers (PERF-T0b).

Background: PERF-T0 migrated everything (inventory + describe) to the
``google-cloud-asset`` SDK's list_assets call. That worked for inventory
but ``Asset.resource.data`` is a SUMMARY -- it's missing fields that
the per-service describe APIs return. Result: the LLM got sparse
input and generated incomplete HCL (e.g., ``google_storage_bucket``
generated with only ``name``, missing required ``location``).

The CLI worked fine because it called ``gcloud <service> describe``
which hits the per-service REST APIs directly, returning full state.

PERF-T0b: restore CLI parity by routing each ``tf_type`` to the
matching per-service Python SDK. Each handler returns a dict shaped
to match the OLD ``gcloud <service> describe`` JSON output, so the
LLM sees the same field names + values it saw on the CLI.

Design pattern (matches Firefly / ControlMonkey / Terraformer):

  * Inventory listing: Cloud Asset Inventory (cheap, batched, fine
    for "what exists")
  * Per-resource describe: per-service SDK (full state, source of truth)

Handler contract:

  describe_<type>(project_id: str, name: str, **extras) -> dict | None

  Returns the gcloud-shaped dict. None if not found / failure.

  ``**extras`` carries parent identifiers for nested resources
  (e.g., zone for compute_instance, cluster for node_pool).

Why each handler is separate (not generic): each per-service SDK has
a slightly different client API shape (``client.get_bucket(name)`` vs
``client.get(GetInstanceRequest(...))`` vs ``client.list_clusters(...)``
filtered client-side). The thin wrapper normalises them all to the
same dict shape. ~10-20 LOC per handler.

PERF-T0b v1 ships handlers for the 3 most-common types (storage_bucket,
compute_instance, container_cluster). Verify on smoke that LLM output
matches CLI, then mass-produce the remaining 13 in a follow-up commit.
Until those land, those types fall back to the existing asset_v1 path
(sparse HCL; needs_attention bucket -- same as today).
"""

from __future__ import annotations

import json
from typing import Any, Callable, Optional

from google.protobuf.json_format import MessageToDict
from google.api_core import exceptions as gcs_exceptions

from common.logging import get_logger

_log = get_logger(__name__)


# ----------------------------------------------------------------------
# SDK client singletons (lazy-initialised; ADC via Cloud Run metadata)
# ----------------------------------------------------------------------

# All clients lazy-initialised (ADC via Cloud Run metadata server).
_storage_client = None
_compute_instances_client = None
_compute_disks_client = None
_compute_firewalls_client = None
_compute_networks_client = None
_compute_subnetworks_client = None
_compute_addresses_client = None
_compute_global_addresses_client = None
_compute_instance_templates_client = None
_container_clusters_client = None
_kms_client = None
_pubsub_publisher_client = None
_pubsub_subscriber_client = None
_iam_client = None
_sql_admin_client = None  # googleapiclient discovery (no first-class SDK)
_run_admin_client = None  # googleapiclient discovery (google-cloud-run
                           # had protobuf<6.0 conflict with our pinned
                           # protobuf 6.x; same pattern as SQL)


def _get_storage_client():
    global _storage_client
    if _storage_client is None:
        from google.cloud import storage
        _storage_client = storage.Client()
    return _storage_client


def _get_compute_instances_client():
    global _compute_instances_client
    if _compute_instances_client is None:
        from google.cloud import compute_v1
        _compute_instances_client = compute_v1.InstancesClient()
    return _compute_instances_client


def _get_compute_disks_client():
    global _compute_disks_client
    if _compute_disks_client is None:
        from google.cloud import compute_v1
        _compute_disks_client = compute_v1.DisksClient()
    return _compute_disks_client


def _get_compute_firewalls_client():
    global _compute_firewalls_client
    if _compute_firewalls_client is None:
        from google.cloud import compute_v1
        _compute_firewalls_client = compute_v1.FirewallsClient()
    return _compute_firewalls_client


def _get_compute_networks_client():
    global _compute_networks_client
    if _compute_networks_client is None:
        from google.cloud import compute_v1
        _compute_networks_client = compute_v1.NetworksClient()
    return _compute_networks_client


def _get_compute_subnetworks_client():
    global _compute_subnetworks_client
    if _compute_subnetworks_client is None:
        from google.cloud import compute_v1
        _compute_subnetworks_client = compute_v1.SubnetworksClient()
    return _compute_subnetworks_client


def _get_compute_addresses_client():
    global _compute_addresses_client
    if _compute_addresses_client is None:
        from google.cloud import compute_v1
        _compute_addresses_client = compute_v1.AddressesClient()
    return _compute_addresses_client


def _get_compute_global_addresses_client():
    global _compute_global_addresses_client
    if _compute_global_addresses_client is None:
        from google.cloud import compute_v1
        _compute_global_addresses_client = compute_v1.GlobalAddressesClient()
    return _compute_global_addresses_client


def _get_compute_instance_templates_client():
    global _compute_instance_templates_client
    if _compute_instance_templates_client is None:
        from google.cloud import compute_v1
        _compute_instance_templates_client = compute_v1.InstanceTemplatesClient()
    return _compute_instance_templates_client


def _get_container_clusters_client():
    global _container_clusters_client
    if _container_clusters_client is None:
        from google.cloud import container_v1
        _container_clusters_client = container_v1.ClusterManagerClient()
    return _container_clusters_client


def _get_kms_client():
    global _kms_client
    if _kms_client is None:
        from google.cloud import kms_v1
        _kms_client = kms_v1.KeyManagementServiceClient()
    return _kms_client


def _get_pubsub_publisher_client():
    global _pubsub_publisher_client
    if _pubsub_publisher_client is None:
        from google.cloud import pubsub_v1
        _pubsub_publisher_client = pubsub_v1.PublisherClient()
    return _pubsub_publisher_client


def _get_pubsub_subscriber_client():
    global _pubsub_subscriber_client
    if _pubsub_subscriber_client is None:
        from google.cloud import pubsub_v1
        _pubsub_subscriber_client = pubsub_v1.SubscriberClient()
    return _pubsub_subscriber_client


def _get_iam_client():
    global _iam_client
    if _iam_client is None:
        from google.cloud import iam_admin_v1
        _iam_client = iam_admin_v1.IAMClient()
    return _iam_client


def _get_run_admin_client():
    """Cloud Run Admin API via googleapiclient discovery.

    google-cloud-run was dropped due to a protobuf version conflict
    with the rest of our google-cloud-* stack. Discovery client is
    a stable fallback that hits the same run.googleapis.com endpoints
    gcloud uses.
    """
    global _run_admin_client
    if _run_admin_client is None:
        from googleapiclient.discovery import build
        _run_admin_client = build(
            "run", "v2", cache_discovery=False,
        )
    return _run_admin_client


def _get_sql_admin_client():
    """Cloud SQL Admin API has no first-class google-cloud-* SDK; use
    the googleapiclient discovery-based pattern."""
    global _sql_admin_client
    if _sql_admin_client is None:
        from googleapiclient.discovery import build
        # cache_discovery=False avoids a noisy ImportError warning when
        # oauth2client isn't installed (we use google-auth instead).
        _sql_admin_client = build(
            "sqladmin", "v1", cache_discovery=False,
        )
    return _sql_admin_client


# ----------------------------------------------------------------------
# Snake_case -> camelCase converter
# ----------------------------------------------------------------------

def _to_camel(snake: str) -> str:
    """Convert snake_case to camelCase. Single-word strings unchanged."""
    parts = snake.split("_")
    if len(parts) == 1:
        return snake
    return parts[0] + "".join(p.title() for p in parts[1:])


def _camelize_keys(obj: Any) -> Any:
    """Recursively convert dict keys snake_case -> camelCase.

    Matches gcloud's JSON output convention so the LLM (which was
    trained on / tested against gcloud's camelCase output via SMOKE 4)
    sees the same field names. Lists are walked element-by-element.
    Non-dict, non-list values pass through unchanged.
    """
    if isinstance(obj, dict):
        return {_to_camel(k): _camelize_keys(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_camelize_keys(item) for item in obj]
    return obj


def _proto_to_camel_dict(proto_msg: Any) -> dict:
    """Convert a proto-plus message to a camelCase dict matching gcloud output.

    google-cloud-* SDKs return proto-plus wrapped messages. MessageToDict
    with ``preserving_proto_field_name=False`` already emits camelCase,
    which matches gcloud. Belt-and-braces: if the SDK ever returns a
    plain dict (some helpers do), pass it through _camelize_keys instead.
    """
    if isinstance(proto_msg, dict):
        return _camelize_keys(proto_msg)
    target = getattr(proto_msg, "_pb", proto_msg)
    try:
        return MessageToDict(target, preserving_proto_field_name=False)
    except Exception:
        # Fall back to JSON round-trip via proto-plus's serializer.
        try:
            return json.loads(type(proto_msg).to_json(proto_msg))
        except Exception as e:
            _log.warning(
                "proto_to_dict_failed",
                msg_type=type(proto_msg).__name__,
                error=str(e)[:200],
            )
            return {}


# ----------------------------------------------------------------------
# Per-type describe handlers
# ----------------------------------------------------------------------

def _describe_storage_bucket(
    project_id: str, name: str, **_extras: Any,
) -> Optional[dict]:
    """Fetch full state for a Cloud Storage bucket.

    Mirrors ``gcloud storage buckets describe gs://<name>`` JSON output:
    name, location, storageClass, lifecycle, iamConfiguration, encryption,
    versioning, labels, retentionPolicy, etc. All fields the LLM needs
    to generate a complete ``google_storage_bucket`` HCL block.
    """
    client = _get_storage_client()
    try:
        bucket = client.get_bucket(name)
    except gcs_exceptions.NotFound:
        _log.info(
            "describe_bucket_not_found",
            project_id=project_id, name=name,
        )
        return None
    # Bucket has a `_properties` dict with the FULL REST API representation
    # (this is what gcloud emits). camelCase already.
    return dict(bucket._properties)


def _describe_compute_instance(
    project_id: str, name: str, **extras: Any,
) -> Optional[dict]:
    """Fetch full state for a Compute Engine VM.

    Requires ``zone`` in extras (compute instances are zonal).
    Mirrors ``gcloud compute instances describe <name> --zone=<zone>``
    JSON output: machineType, networkInterfaces, disks, metadata, tags,
    serviceAccounts, scheduling, etc.
    """
    zone = extras.get("zone") or extras.get("location")
    if not zone:
        _log.error(
            "describe_instance_missing_zone",
            project_id=project_id, name=name, extras=str(extras)[:200],
        )
        return None
    client = _get_compute_instances_client()
    try:
        from google.cloud import compute_v1
        instance = client.get(
            project=project_id, zone=zone, instance=name,
        )
    except gcs_exceptions.NotFound:
        return None
    return _proto_to_camel_dict(instance)


def _describe_container_cluster(
    project_id: str, name: str, **extras: Any,
) -> Optional[dict]:
    """Fetch full state for a GKE cluster.

    Requires ``location`` in extras (cluster's region OR zone).
    Mirrors ``gcloud container clusters describe <name> --location=<loc>``
    JSON output: nodePools, network, masterAuth, addonsConfig, etc.
    """
    location = extras.get("location") or extras.get("zone") or extras.get("region")
    if not location:
        _log.error(
            "describe_cluster_missing_location",
            project_id=project_id, name=name, extras=str(extras)[:200],
        )
        return None
    client = _get_container_clusters_client()
    cluster_path = f"projects/{project_id}/locations/{location}/clusters/{name}"
    try:
        from google.cloud import container_v1
        cluster = client.get_cluster(name=cluster_path)
    except gcs_exceptions.NotFound:
        return None
    return _proto_to_camel_dict(cluster)


# ----------------------------------------------------------------------
# PERF-T0b v2: 13 remaining handlers
# ----------------------------------------------------------------------

def _describe_compute_disk(
    project_id: str, name: str, **extras: Any,
) -> Optional[dict]:
    """Compute Engine persistent disk (zonal)."""
    zone = extras.get("zone") or extras.get("location")
    if not zone:
        _log.error("describe_disk_missing_zone",
                   project_id=project_id, name=name)
        return None
    client = _get_compute_disks_client()
    try:
        disk = client.get(project=project_id, zone=zone, disk=name)
    except gcs_exceptions.NotFound:
        return None
    return _proto_to_camel_dict(disk)


def _describe_compute_firewall(
    project_id: str, name: str, **_extras: Any,
) -> Optional[dict]:
    """Compute Engine firewall rule (global)."""
    client = _get_compute_firewalls_client()
    try:
        fw = client.get(project=project_id, firewall=name)
    except gcs_exceptions.NotFound:
        return None
    return _proto_to_camel_dict(fw)


def _describe_compute_network(
    project_id: str, name: str, **_extras: Any,
) -> Optional[dict]:
    """VPC network (global)."""
    client = _get_compute_networks_client()
    try:
        net = client.get(project=project_id, network=name)
    except gcs_exceptions.NotFound:
        return None
    return _proto_to_camel_dict(net)


def _describe_compute_subnetwork(
    project_id: str, name: str, **extras: Any,
) -> Optional[dict]:
    """VPC subnetwork (regional)."""
    region = extras.get("region") or extras.get("location")
    if not region:
        _log.error("describe_subnetwork_missing_region",
                   project_id=project_id, name=name)
        return None
    client = _get_compute_subnetworks_client()
    try:
        sub = client.get(
            project=project_id, region=region, subnetwork=name,
        )
    except gcs_exceptions.NotFound:
        return None
    return _proto_to_camel_dict(sub)


def _describe_compute_address(
    project_id: str, name: str, **extras: Any,
) -> Optional[dict]:
    """Static IP address (regional OR global).

    Heuristic: if region is provided -> regional client; else -> global.
    Matches gcloud's behavior (regional addresses use --region, global
    addresses use --global).
    """
    region = extras.get("region") or extras.get("location")
    if region and region != "global":
        client = _get_compute_addresses_client()
        try:
            addr = client.get(
                project=project_id, region=region, address=name,
            )
        except gcs_exceptions.NotFound:
            return None
        return _proto_to_camel_dict(addr)
    # Global address path
    client = _get_compute_global_addresses_client()
    try:
        addr = client.get(project=project_id, address=name)
    except gcs_exceptions.NotFound:
        return None
    return _proto_to_camel_dict(addr)


def _describe_compute_instance_template(
    project_id: str, name: str, **_extras: Any,
) -> Optional[dict]:
    """Compute Engine instance template (global)."""
    client = _get_compute_instance_templates_client()
    try:
        tmpl = client.get(project=project_id, instance_template=name)
    except gcs_exceptions.NotFound:
        return None
    return _proto_to_camel_dict(tmpl)


def _describe_container_node_pool(
    project_id: str, name: str, **extras: Any,
) -> Optional[dict]:
    """GKE node pool (nested under a cluster).

    Requires ``location`` AND ``cluster`` in extras.
    """
    location = extras.get("location") or extras.get("zone") or extras.get("region")
    cluster = extras.get("cluster")
    if not location or not cluster:
        _log.error(
            "describe_node_pool_missing_parent",
            project_id=project_id, name=name,
            location=location, cluster=cluster,
        )
        return None
    client = _get_container_clusters_client()
    np_path = (
        f"projects/{project_id}/locations/{location}/clusters/"
        f"{cluster}/nodePools/{name}"
    )
    try:
        np = client.get_node_pool(name=np_path)
    except gcs_exceptions.NotFound:
        return None
    return _proto_to_camel_dict(np)


def _describe_kms_key_ring(
    project_id: str, name: str, **extras: Any,
) -> Optional[dict]:
    """KMS key ring (regional).

    Requires ``location`` in extras.
    """
    location = extras.get("location") or extras.get("region")
    if not location:
        _log.error("describe_keyring_missing_location",
                   project_id=project_id, name=name)
        return None
    client = _get_kms_client()
    kr_path = (
        f"projects/{project_id}/locations/{location}/keyRings/{name}"
    )
    try:
        kr = client.get_key_ring(name=kr_path)
    except gcs_exceptions.NotFound:
        return None
    return _proto_to_camel_dict(kr)


def _describe_kms_crypto_key(
    project_id: str, name: str, **extras: Any,
) -> Optional[dict]:
    """KMS crypto key (nested under a key ring).

    Requires ``location`` AND ``keyring`` in extras.
    """
    location = extras.get("location") or extras.get("region")
    keyring = extras.get("keyring") or extras.get("key_ring")
    if not location or not keyring:
        _log.error(
            "describe_crypto_key_missing_parent",
            project_id=project_id, name=name,
            location=location, keyring=keyring,
        )
        return None
    client = _get_kms_client()
    ck_path = (
        f"projects/{project_id}/locations/{location}/keyRings/"
        f"{keyring}/cryptoKeys/{name}"
    )
    try:
        ck = client.get_crypto_key(name=ck_path)
    except gcs_exceptions.NotFound:
        return None
    return _proto_to_camel_dict(ck)


def _describe_pubsub_topic(
    project_id: str, name: str, **_extras: Any,
) -> Optional[dict]:
    """Pub/Sub topic (project-scoped, no location)."""
    client = _get_pubsub_publisher_client()
    topic_path = f"projects/{project_id}/topics/{name}"
    try:
        topic = client.get_topic(request={"topic": topic_path})
    except gcs_exceptions.NotFound:
        return None
    return _proto_to_camel_dict(topic)


def _describe_pubsub_subscription(
    project_id: str, name: str, **_extras: Any,
) -> Optional[dict]:
    """Pub/Sub subscription (project-scoped)."""
    client = _get_pubsub_subscriber_client()
    sub_path = f"projects/{project_id}/subscriptions/{name}"
    try:
        sub = client.get_subscription(request={"subscription": sub_path})
    except gcs_exceptions.NotFound:
        return None
    return _proto_to_camel_dict(sub)


def _describe_sql_database_instance(
    project_id: str, name: str, **_extras: Any,
) -> Optional[dict]:
    """Cloud SQL instance.

    Uses the discovery-based googleapiclient (no first-class
    google-cloud-sql-admin SDK exists). Returns the dict directly --
    googleapiclient already gives camelCase JSON.
    """
    client = _get_sql_admin_client()
    try:
        return client.instances().get(
            project=project_id, instance=name,
        ).execute()
    except Exception as e:
        # googleapiclient raises HttpError on 404; treat any failure
        # as not-found here (genuine errors propagate to the caller's
        # except in get_resource_details_json).
        if "404" in str(e) or "Not Found" in str(e):
            return None
        raise


def _describe_service_account(
    project_id: str, name: str, **_extras: Any,
) -> Optional[dict]:
    """IAM Service Account.

    `name` should be the SA email (e.g. "poc-sa@p.iam.gserviceaccount.com").
    The SA URN format is `projects/<P>/serviceAccounts/<email>`.
    """
    client = _get_iam_client()
    sa_path = f"projects/{project_id}/serviceAccounts/{name}"
    try:
        from google.cloud import iam_admin_v1
        sa = client.get_service_account(
            request={"name": sa_path},
        )
    except gcs_exceptions.NotFound:
        return None
    return _proto_to_camel_dict(sa)


def _describe_cloud_run_v2_service(
    project_id: str, name: str, **extras: Any,
) -> Optional[dict]:
    """Cloud Run v2 service (regional).

    Requires ``location`` in extras. Uses googleapiclient (run/v2
    discovery API) -- google-cloud-run was dropped due to protobuf
    version conflict with our pinned protobuf 6.x stack. Returns the
    dict directly (already camelCase from the REST API).
    """
    location = extras.get("location") or extras.get("region")
    if not location:
        _log.error("describe_cloud_run_missing_location",
                   project_id=project_id, name=name)
        return None
    client = _get_run_admin_client()
    svc_path = (
        f"projects/{project_id}/locations/{location}/services/{name}"
    )
    try:
        return client.projects().locations().services().get(
            name=svc_path,
        ).execute()
    except Exception as e:
        # googleapiclient raises HttpError on 404 -- treat as not-found.
        # Other failures propagate to gcp_client's outer except.
        if "404" in str(e) or "Not Found" in str(e):
            return None
        raise


# ----------------------------------------------------------------------
# Dispatch table
# ----------------------------------------------------------------------

# Maps tf_type -> per-service describe handler. Each handler returns
# the gcloud-shaped dict OR None on failure. Types not in this table
# fall back to the asset_v1 path (sparse data; LLM may produce
# incomplete HCL -- ships as needs_attention).
#
# PERF-T0b v1 covers 3 most-common types. Future commits add the
# remaining 13 (compute_disk, compute_firewall, compute_network,
# compute_subnetwork, compute_address, compute_instance_template,
# container_node_pool, kms_key_ring, kms_crypto_key, pubsub_topic,
# pubsub_subscription, sql_database_instance, service_account,
# cloud_run_v2_service).
_HANDLERS: dict = {
    # PERF-T0b v1 (verified end-to-end via PUI-1B smoke 2026-04-28)
    "google_storage_bucket": _describe_storage_bucket,
    "google_compute_instance": _describe_compute_instance,
    "google_container_cluster": _describe_container_cluster,
    # PERF-T0b v2 (mass-produced from the v1 pattern)
    "google_compute_disk": _describe_compute_disk,
    "google_compute_firewall": _describe_compute_firewall,
    "google_compute_network": _describe_compute_network,
    "google_compute_subnetwork": _describe_compute_subnetwork,
    "google_compute_address": _describe_compute_address,
    "google_compute_instance_template": _describe_compute_instance_template,
    "google_container_node_pool": _describe_container_node_pool,
    "google_kms_key_ring": _describe_kms_key_ring,
    "google_kms_crypto_key": _describe_kms_crypto_key,
    "google_pubsub_topic": _describe_pubsub_topic,
    "google_pubsub_subscription": _describe_pubsub_subscription,
    "google_sql_database_instance": _describe_sql_database_instance,
    "google_service_account": _describe_service_account,
    "google_cloud_run_v2_service": _describe_cloud_run_v2_service,
}


def get_handler(tf_type: str) -> Optional[Callable]:
    """Return the per-service describe handler for a tf_type, or None
    if no handler exists yet (fall back to asset_v1)."""
    return _HANDLERS.get(tf_type)


def describe(
    tf_type: str,
    project_id: str,
    name: str,
    **extras: Any,
) -> Optional[dict]:
    """Dispatch a describe call to the per-service handler.

    Args:
        tf_type: The Terraform resource type (e.g. "google_storage_bucket").
        project_id: GCP project.
        name: Resource short name (NOT the full URN).
        **extras: Per-type extra identifiers -- zone for compute,
            location for GKE, cluster for node pools, keyring for
            crypto keys, etc.

    Returns:
        Dict matching gcloud describe output (camelCase keys), or
        None if no handler OR resource not found.
    """
    handler = _HANDLERS.get(tf_type)
    if not handler:
        return None
    return handler(project_id, name, **extras)
