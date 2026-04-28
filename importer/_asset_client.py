# importer/_asset_client.py
"""Cloud Asset Inventory SDK helpers (PERF-T0 engine SDK migration).

Replaces the prior subprocess `gcloud asset search-all-resources` and
per-resource `gcloud <service> describe` calls with native Python SDK
calls (``google-cloud-asset``).

Why this module exists:

  * The PUI-1 SMOKE on Cloud Run surfaced two consecutive issues with
    the subprocess approach:
      - importer/config.py hardcoded a Windows path for the gcloud
        binary (`C:\\Program Files (x86)\\...`), which doesn't exist
        in our Linux Cloud Run container.
      - Even with the path fixed, gcloud CLI in containers needs an
        explicit `gcloud auth login` / `gcloud config set account`
        chain plus a working credential store -- a brittle dependency
        on metadata-server detection heuristics that vary by SDK
        version. (The earlier common/storage.py SDK rewrite addressed
        the same issue at the storage layer.)

  * Python SDKs use Application Default Credentials (ADC) cleanly via
    Cloud Run's metadata server, with NO `gcloud auth` dance required.
    Same approach already proven in google-cloud-storage,
    google-cloud-aiplatform, etc.

  * One SDK (google-cloud-asset) covers BOTH the discover and describe
    operations -- avoids pulling per-service SDKs for compute, storage,
    container, kms, pubsub, sql separately.

Architectural pattern:

  * ``_get_asset_client()`` -- lazy module-singleton. Tests patch this
    seam; production gets a real ADC-backed AssetServiceClient.
  * ``list_resources_of_type(project_id, asset_type)`` -- replaces the
    old ``gcloud asset search-all-resources --asset-types=X`` call.
    Returns a list of dicts in the SAME shape gcloud returned, so
    downstream (inventory.py, gcp_client._map_asset_to_terraform) is
    unchanged.
  * ``get_resource_state(project_id, asset_type, asset_name)`` --
    replaces the per-type ``gcloud <service> describe`` calls. Returns
    the resource's full JSON state, equivalent to gcloud's output.

Why list_assets over search_all_resources:

  * ``list_assets(content_type=RESOURCE)`` returns the full
    ``Asset.resource.data`` Struct (the gcloud-describe equivalent).
  * ``search_all_resources`` returns ``ResourceSearchResult`` which has
    fewer fields by default and requires explicit ``read_mask`` to get
    full state -- more fragile.
  * Both have the same auth requirements (cloudasset.googleapis.com API
    enabled + ``roles/cloudasset.viewer`` on the target project).
"""

from __future__ import annotations

import json
from typing import Any, Optional

from google.cloud import asset_v1
from google.api_core import exceptions as gcs_exceptions
from google.protobuf.json_format import MessageToDict

from common.logging import get_logger

_log = get_logger(__name__)

# Module-singleton AssetServiceClient. Lazily constructed on first use
# so import-time cost is zero (matters for unit tests that don't touch
# the SDK). Thread-safe per the Google Cloud SDK docs.
_asset_client: Optional["asset_v1.AssetServiceClient"] = None


def _get_asset_client() -> "asset_v1.AssetServiceClient":
    """Return the module-singleton AssetServiceClient.

    Lazy-initialized; uses ADC (auto-detected via Cloud Run metadata
    server). Same pattern as common.storage._get_gcs_client.

    Pulled into a helper so tests can patch a single seam:
    ``patch("importer._asset_client._get_asset_client", return_value=mock)``.
    """
    global _asset_client
    if _asset_client is None:
        _asset_client = asset_v1.AssetServiceClient()
    return _asset_client


def _asset_to_legacy_dict(asset: Any) -> dict:
    """Convert a google.cloud.asset_v1 Asset to the dict shape the
    rest of the importer expects.

    The legacy gcloud asset output looked like::

        {
          "name": "//compute.googleapis.com/projects/.../instances/vm-a",
          "assetType": "compute.googleapis.com/Instance",
          "displayName": "vm-a",
          "location": "us-central1-a",
          ...
        }

    The SDK's Asset.resource.data is a Struct (the full describe output);
    we flatten the top-level Asset metadata + the resource.data into
    one dict that downstream consumers handle without changes.

    The Struct conversion uses ``MessageToDict`` with
    ``preserving_proto_field_name=False`` so field names match the
    REST API's camelCase (matches what gcloud emitted).
    """
    out = {
        "name": asset.name,
        "assetType": asset.asset_type,
    }
    if asset.resource and asset.resource.data:
        # Resource.data is a google.protobuf.Struct -- convert to dict.
        # MessageToDict on a Struct returns a flat dict of its fields.
        data_dict = MessageToDict(
            asset.resource.data._pb if hasattr(asset.resource.data, "_pb")
            else asset.resource.data,
            preserving_proto_field_name=False,
        )
        # Merge top-level resource fields. displayName + location often
        # live inside the data; pull them up so downstream consumers
        # find them at the top level (matching gcloud's flat output).
        if "name" in data_dict and "/" in data_dict["name"]:
            out["displayName"] = data_dict["name"].rsplit("/", 1)[-1]
        elif "displayName" in data_dict:
            out["displayName"] = data_dict["displayName"]
        else:
            # Fall back to last segment of the asset URN
            out["displayName"] = asset.name.rsplit("/", 1)[-1]
        if "location" in data_dict:
            out["location"] = data_dict["location"]
        elif "zone" in data_dict:
            # Compute resources expose `zone` as a URL; extract last segment
            out["location"] = data_dict["zone"].rsplit("/", 1)[-1]
        elif "region" in data_dict:
            out["location"] = data_dict["region"].rsplit("/", 1)[-1]
        # Preserve everything else from data so describe-style consumers
        # see the full state (this is what makes the SDK migration
        # equivalent to the old subprocess gcloud describe output).
        out.update({k: v for k, v in data_dict.items() if k not in out})
    return out


def list_resources_of_type(project_id: str, asset_type: str) -> list[dict]:
    """List all resources of an asset type in a project.

    Replaces the legacy ``gcloud asset search-all-resources
    --asset-types=X`` call. Returns the SAME dict shape so callers
    (inventory.py, gcp_client._map_asset_to_terraform) don't change.

    Args:
        project_id: GCP project ID being scanned.
        asset_type: Cloud Asset type, e.g. "compute.googleapis.com/Instance".

    Returns:
        List of dicts. Each dict has at least ``name``, ``assetType``,
        ``displayName``, and ``location`` (when applicable), plus the
        full resource state under named fields. Empty list if no
        resources of that type exist OR the listing returned nothing.

    Raises:
        google.api_core.exceptions.PermissionDenied: runtime SA
            lacks ``roles/cloudasset.viewer`` on the target project.
        google.api_core.exceptions.NotFound: the project doesn't
            exist OR the cloudasset.googleapis.com API isn't enabled.
        Other google.api_core.exceptions for transient errors.
    """
    client = _get_asset_client()
    parent = f"projects/{project_id}"
    _log.info(
        "asset_list_start",
        project_id=project_id,
        asset_type=asset_type,
    )
    try:
        # content_type=RESOURCE returns the full describe-style data;
        # without it we'd get only metadata (name + asset_type).
        page = client.list_assets(
            request={
                "parent": parent,
                "asset_types": [asset_type],
                "content_type": asset_v1.ContentType.RESOURCE,
            },
        )
        results = [_asset_to_legacy_dict(asset) for asset in page]
    except gcs_exceptions.NotFound:
        # Project missing OR cloudasset.googleapis.com API not enabled.
        # Surface as empty list with INFO log -- inventory's per-asset-
        # type try/except will count this as a per-type failure but
        # the overall workflow continues.
        _log.info(
            "asset_list_not_found",
            project_id=project_id,
            asset_type=asset_type,
            reason="project missing OR cloudasset API not enabled",
        )
        return []
    except gcs_exceptions.PermissionDenied as e:
        _log.error(
            "asset_list_permission_denied",
            project_id=project_id,
            asset_type=asset_type,
            error=str(e)[:300],
            hint=(
                "Runtime SA needs roles/cloudasset.viewer on this "
                "project (or roles/viewer which includes it). Run "
                "scripts/onboard_customer_project.sh."
            ),
        )
        raise

    _log.info(
        "asset_list_complete",
        project_id=project_id,
        asset_type=asset_type,
        count=len(results),
    )
    return results


def get_resource_state(
    project_id: str,
    asset_type: str,
    asset_name: str,
) -> Optional[dict]:
    """Fetch the full state for a single resource.

    Replaces the legacy per-type ``gcloud <service> describe`` calls.
    Returns the same dict shape gcloud emitted so downstream
    snapshot_scrubber + LLM prompt building are unchanged.

    Args:
        project_id: GCP project the resource lives in.
        asset_type: Cloud Asset type (e.g. "compute.googleapis.com/Instance").
        asset_name: Either the short name (e.g. "vm-a") OR the full
            URN ("//compute.googleapis.com/projects/.../instances/vm-a").
            We match on the trailing segment when given a short name.

    Returns:
        Dict with the full resource state, or None if no matching
        resource was found in the inventory listing.

    Raises:
        Same as list_resources_of_type.

    Implementation note: list_assets doesn't expose a name filter, so
    we list ALL of the asset_type and find the matching one client-side.
    Acceptable for our scale (single-digit to low-hundreds of resources
    per project). At higher scale we'd switch to BatchGetAssetsHistory.
    """
    candidates = list_resources_of_type(project_id, asset_type)
    # Match on either full URN OR trailing-segment short name.
    short_target = asset_name.rsplit("/", 1)[-1]
    for candidate in candidates:
        cname = candidate.get("name", "")
        cdisplay = candidate.get("displayName", "")
        if cname == asset_name:
            return candidate
        if cdisplay == asset_name:
            return candidate
        if cname.rsplit("/", 1)[-1] == short_target:
            return candidate
    _log.warning(
        "asset_get_not_found",
        project_id=project_id,
        asset_type=asset_type,
        asset_name=asset_name,
        candidates=len(candidates),
        reason="no asset with matching name OR displayName in listing",
    )
    return None


def get_resource_state_as_json(
    project_id: str,
    asset_type: str,
    asset_name: str,
) -> Optional[str]:
    """JSON-serialised wrapper around ``get_resource_state``.

    Returns the same JSON string shape that the old subprocess
    ``gcloud describe ... --format=json`` produced -- a top-level
    dict serialised with default JSON formatting. ``None`` when no
    matching resource is found (caller renders as "describe failed").

    Used by importer/gcp_client.get_resource_details_json to keep
    that function's downstream contract (str-or-None) identical.
    """
    state = get_resource_state(project_id, asset_type, asset_name)
    if state is None:
        return None
    return json.dumps(state, indent=2)
