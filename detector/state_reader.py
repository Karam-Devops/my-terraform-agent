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
        # Most GCP types store the project under a top-level `project`
        # attribute in state. Cheap happy-path lookup first.
        proj = self.attributes.get("project")
        if proj:
            return proj
        # D-3 fix (2026-04-28): some types don't have a top-level
        # `project` -- it's encoded in a parent URN instead. The known
        # case is google_kms_crypto_key, where `key_ring` is
        # `projects/<P>/locations/<L>/keyRings/<K>` and the canonical
        # `id` is `projects/<P>/locations/<L>/keyRings/<K>/cryptoKeys/<X>`.
        # Pre-fix, the detector printed
        #   "⚠️  google_kms_crypto_key.poc_key has no 'project'
        #    attribute in state. Skipping."
        # then skipped the describe -> resource showed up as
        # "missing snapshot" -> downstream a cosmetic in-sync display
        # via the drift-stub path (drift-stub types report has_drift
        # = False even with a missing snapshot, masking the noise).
        # Cosmetic in the report, but each missed snapshot also fires
        # a LOW `cloud_snapshot_missing` finding in the Policy stage.
        # Extracting from `id` resolves both: describe call succeeds,
        # the LOW finding goes away.
        rid = self.attributes.get("id", "")
        if isinstance(rid, str) and rid.startswith("projects/"):
            parts = rid.split("/", 2)
            # parts == ["projects", "<P>", "<rest-of-path>"]
            if len(parts) >= 2 and parts[1]:
                return parts[1]
        return None

    @property
    def location(self) -> Optional[str]:
        # The "location" concept lives under different state-attribute
        # names depending on the resource type. Three top-level buckets
        # plus a URN-extraction fallback:
        #   * "zone"     — google_compute_instance, google_compute_disk
        #                  (anything zonal)
        #   * "location" — google_storage_bucket, google_kms_key_ring,
        #                  google_kms_crypto_key (multi-region or
        #                  region-or-zone-agnostic types)
        #   * "region"   — google_compute_subnetwork, google_compute_address
        #                  (regional-only types)
        #   * URN extract — google_kms_crypto_key in particular doesn't
        #                  have ANY top-level location attribute. Its
        #                  location lives inside `key_ring` (the parent
        #                  URN: `projects/<P>/locations/<L>/keyRings/<K>`)
        #                  AND inside `id` (`projects/<P>/locations/<L>/
        #                  keyRings/<K>/cryptoKeys/<X>`). Parse either.
        #
        # D-1 fix (2026-04-28): added `region` to the fallback chain
        # for google_compute_subnetwork.
        # D-3 round 2 (same day, after smoke surfaced gcloud "Failed
        # to find attribute [location]" on crypto_key describe):
        # added URN-extraction fallback so crypto_key can populate
        # the --location flag from its parent URN.
        direct = (
            self.attributes.get("zone")
            or self.attributes.get("location")
            or self.attributes.get("region")
        )
        if direct:
            return direct
        # URN fallback: pattern is `projects/<P>/locations/<L>/...`.
        # `key_ring` is the most specific parent URN (no extra path
        # segments after the location part to introduce ambiguity);
        # try it first, then fall back to `id`.
        for attr in ("key_ring", "id"):
            urn = self.attributes.get(attr, "")
            if isinstance(urn, str):
                parts = urn.split("/")
                # parts == ["projects", "<P>", "locations", "<L>", ...]
                if (len(parts) >= 4
                        and parts[0] == "projects"
                        and parts[2] == "locations"
                        and parts[3]):
                    return parts[3]
        return None

    @property
    def keyring(self) -> Optional[str]:
        """Parent keyring name for google_kms_crypto_key.

        State's `key_ring` attribute is the full URN
        (`projects/<P>/locations/<L>/keyRings/<K>`), but
        ``gcloud kms keys describe`` expects just the keyring name
        (e.g. ``poc-keyring``) via the ``--keyring`` flag.

        D-3 round 2 (2026-04-28): the detector's ``_build_mapping``
        wasn't surfacing the keyring parent at all. ``gcp_client``
        already has the wiring to thread ``mapping["keyring"]`` into
        the ``--keyring`` flag (see ``importer/gcp_client.py`` line
        ~218); we just needed to populate the mapping key from state.
        Type-scoped to crypto_key -- other types don't use the
        keyring concept.
        """
        if self.tf_type != "google_kms_crypto_key":
            return None
        key_ring = self.attributes.get("key_ring", "")
        if isinstance(key_ring, str):
            parts = key_ring.split("/")
            # parts == ["projects", "<P>", "locations", "<L>",
            #          "keyRings", "<K>"]
            if (len(parts) >= 6
                    and parts[4] == "keyRings"
                    and parts[5]):
                return parts[5]
        return None

    @property
    def resource_name(self) -> Optional[str]:
        # Most GCP types store the gcloud-friendly short name under
        # `name` (e.g. google_compute_instance.name == "poc-vm",
        # google_kms_key_ring.name == "poc-keyring") -- we pass that
        # straight to `gcloud <type> describe <name>`.
        #
        # D-2 fix (2026-04-28): google_service_account is a known
        # exception. Its `name` attribute is the canonical resource
        # path (`projects/<P>/serviceAccounts/<email>`), but `gcloud
        # iam service-accounts describe` expects JUST the email. Pre-
        # fix, passing the full path made gcloud construct a malformed
        # API URL, hit a 404 with an HTML error page, and the detector
        # silently classified the resource as "missing from cloud" ->
        # "in sync" (false positive drift mask). The importer side
        # works because it extracts just the email during asset
        # enumeration; the detector reads from state where the full
        # path is stored.
        #
        # Per-type override is the right shape here -- broadening to
        # `email or name` would risk surprises in any future resource
        # that happens to have a stray top-level `email` attribute
        # unrelated to the gcloud describe key.
        if self.tf_type == "google_service_account":
            email = self.attributes.get("email")
            if email:
                return email
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