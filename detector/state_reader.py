# detector/state_reader.py
"""
Reads a local terraform.tfstate file and yields one ManagedResource per
resource instance. POC: local file only. GCS backend support is a TODO.
"""

import json
import os
from dataclasses import dataclass
from typing import List, Optional

from . import config


@dataclass
class ManagedResource:
    """A single resource instance as Terraform sees it in state."""
    tf_type: str           # e.g. "google_compute_instance"
    hcl_name: str          # local name in HCL, e.g. "poc_gce"
    tf_address: str        # "<tf_type>.<hcl_name>"
    attributes: dict       # the full state-side attribute dict (snake_case)
    in_scope: bool         # whether the detector will touch it this run

    @property
    def project_id(self) -> Optional[str]:
        return self.attributes.get("project")

    @property
    def location(self) -> Optional[str]:
        # google_compute_instance uses "zone"; google_storage_bucket uses "location"
        return self.attributes.get("zone") or self.attributes.get("location")

    @property
    def resource_name(self) -> Optional[str]:
        return self.attributes.get("name")


def read_state(state_path: str) -> List[ManagedResource]:
    """Parses a tfstate file. Returns an empty list if missing/empty."""
    if not os.path.isfile(state_path):
        print(f"⚠️  State file not found: {state_path}")
        return []

    try:
        with open(state_path, "r", encoding="utf-8") as f:
            state = json.load(f)
    except json.JSONDecodeError as e:
        print(f"❌ State file is not valid JSON: {e}")
        return []

    out: List[ManagedResource] = []
    for resource in state.get("resources", []):
        # We only care about provider-managed resources (skip data sources).
        if resource.get("mode") != "managed":
            continue
        tf_type = resource.get("type")
        hcl_name = resource.get("name")
        if not tf_type or not hcl_name:
            continue

        # Each resource entry can have multiple instances (count/for_each).
        # POC: handle the single-instance common case; surface a warning if more.
        instances = resource.get("instances", [])
        if len(instances) > 1:
            print(f"⚠️  {tf_type}.{hcl_name} has {len(instances)} instances; "
                  f"POC only diffs index 0.")
        if not instances:
            continue

        attrs = instances[0].get("attributes", {}) or {}
        out.append(ManagedResource(
            tf_type=tf_type,
            hcl_name=hcl_name,
            tf_address=f"{tf_type}.{hcl_name}",
            attributes=attrs,
            in_scope=config.is_in_scope(tf_type),
        ))

    return out


def summarize(resources: List[ManagedResource]) -> None:
    in_scope = [r for r in resources if r.in_scope]
    out_scope = [r for r in resources if not r.in_scope]
    print(f"\n📦 State contains {len(resources)} managed resource(s):")
    print(f"   - {len(in_scope)} in scope for drift detection")
    print(f"   - {len(out_scope)} out of scope (will be skipped)")
    for r in in_scope:
        print(f"     ✅ {r.tf_address}")
    for r in out_scope:
        print(f"     ⏭  {r.tf_address}  ({r.tf_type})")