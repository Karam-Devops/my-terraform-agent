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

_storage_client = None
_compute_instances_client = None
_container_clusters_client = None


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


def _get_container_clusters_client():
    global _container_clusters_client
    if _container_clusters_client is None:
        from google.cloud import container_v1
        _container_clusters_client = container_v1.ClusterManagerClient()
    return _container_clusters_client


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
    "google_storage_bucket": _describe_storage_bucket,
    "google_compute_instance": _describe_compute_instance,
    "google_container_cluster": _describe_container_cluster,
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
