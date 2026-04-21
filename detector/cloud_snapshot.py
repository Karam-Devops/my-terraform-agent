# detector/cloud_snapshot.py
"""
Fetches the live cloud JSON for each in-scope managed resource, in parallel.
Reuses importer.gcp_client so we don't fork describe-command knowledge.
"""

import concurrent.futures
import json
from typing import Dict, List, Optional

from importer import gcp_client
from importer import config as importer_config
from . import config
from .state_reader import ManagedResource


def _build_mapping(resource: ManagedResource) -> Optional[dict]:
    """
    Translates a state-side ManagedResource into the 'mapping' dict shape that
    importer.gcp_client.get_resource_details_json expects.
    """
    if resource.tf_type not in importer_config.TF_TYPE_TO_GCLOUD_INFO:
        print(f"⚠️  No describe command registered for {resource.tf_type}. Skipping.")
        return None
    if not resource.resource_name:
        print(f"⚠️  {resource.tf_address} has no 'name' attribute in state. Skipping.")
        return None
    if not resource.project_id:
        print(f"⚠️  {resource.tf_address} has no 'project' attribute in state. Skipping.")
        return None

    return {
        "tf_type": resource.tf_type,
        "hcl_name": resource.hcl_name,
        "resource_name": resource.resource_name,
        "project_id": resource.project_id,
        "location": resource.location,
    }


def _fetch_one(resource: ManagedResource) -> tuple:
    """Worker: fetch live JSON for one resource. Returns (address, dict|None)."""
    mapping = _build_mapping(resource)
    if mapping is None:
        return (resource.tf_address, None)

    raw = gcp_client.get_resource_details_json(mapping)
    if not raw:
        return (resource.tf_address, None)
    try:
        return (resource.tf_address, json.loads(raw))
    except json.JSONDecodeError:
        print(f"❌ Cloud JSON for {resource.tf_address} was not parseable.")
        return (resource.tf_address, None)


def fetch_snapshots(resources: List[ManagedResource]) -> Dict[str, dict]:
    """Fetches live JSON for all in-scope resources in parallel."""
    in_scope = [r for r in resources if r.in_scope]
    if not in_scope:
        return {}

    print(f"\n☁️  Fetching live cloud snapshots for {len(in_scope)} resource(s)...")
    snapshots: Dict[str, dict] = {}

    with concurrent.futures.ThreadPoolExecutor(
        max_workers=config.MAX_SNAPSHOT_WORKERS
    ) as ex:
        futures = {ex.submit(_fetch_one, r): r for r in in_scope}
        for fut in concurrent.futures.as_completed(futures):
            address, data = fut.result()
            snapshots[address] = data  # may be None on failure
    return snapshots