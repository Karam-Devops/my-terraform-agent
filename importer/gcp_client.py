# importer/gcp_client.py
import json
import re
from . import config
from . import shell_runner
from common.logging import get_logger

log = get_logger(__name__)


# C5.1: GCP location strings are EITHER zones (us-central1-a) OR regions
# (us-central1). For dual-mode resources -- GKE clusters and node pools --
# we must pick the right gcloud flag at runtime based on the location's
# shape. Zones always end with `-<single letter>`; regions never do.
# Pre-compiled because describe runs many times per workflow.
_ZONE_LOCATION_RE = re.compile(r"^[a-z]+-[a-z]+\d+-[a-z]$")


def _is_zonal_location(location: str) -> bool:
    """Return True iff `location` looks like a GCP zone (us-central1-a)
    rather than a region (us-central1).

    Pure function so it can be unit-tested without mocking gcloud.
    """
    if not location:
        return False
    return bool(_ZONE_LOCATION_RE.match(location))


def _resolve_location_flag(info: dict, mapping: dict):
    """Pick the right gcloud --zone / --region flag for this describe.

    Resources fall into three groups:
      * Zonal-only (compute_instance, compute_disk):  declares zone_flag
        only -> always emit --zone <location>.
      * Regional-only (compute_subnetwork, compute_address): declares
        region_flag only -> always emit --region <location>.
      * Dual-mode (google_container_cluster, google_container_node_pool):
        declares BOTH flags -> pick based on whether `location` looks
        like a zone or region. Zones get --zone, regions get --region.

    Returns a list of args to extend command_args with, or [] if no
    location flag applies (e.g. global resources like networks/buckets).
    """
    location = mapping.get("location")
    has_zone = "zone_flag" in info
    has_region = "region_flag" in info
    if not location or not (has_zone or has_region):
        return []
    if has_zone and has_region:
        # Dual-mode: pick by location shape.
        flag = info["zone_flag"] if _is_zonal_location(location) else info["region_flag"]
        return [flag, location]
    if has_zone:
        return [info["zone_flag"], location]
    return [info["region_flag"], location]


def discover_resources_of_type(project_id, asset_type):
    log.info("discover_start", project_id=project_id, asset_type=asset_type)
    command_args = (
        config.GCLOUD_CMD_PATH, "--quiet", "asset", "search-all-resources",
        f"--scope=projects/{project_id}", f"--asset-types={asset_type}", "--format=json"
    )
    output = shell_runner.run_command(command_args)
    if not output: return []
    try:
        resources = json.loads(output)
        log.info("discover_complete", asset_type=asset_type, count=len(resources))
        return resources
    except json.JSONDecodeError:
        log.error("discover_parse_failed", asset_type=asset_type,
                  reason="gcloud returned non-JSON output")
        return []

def get_resource_details_json(mapping):
    """Gets the full JSON configuration for a selected resource using 'gcloud describe'."""
    tf_type = mapping["tf_type"]
    log.info("describe_start",
             tf_type=tf_type,
             resource_name=mapping.get("resource_name"))

    # Previously: a debug print dumping the entire TF_TYPE_TO_GCLOUD_INFO
    # dictionary on every describe call. Phase 0 audit flagged this as
    # noise-level output in the hot path (~30 lines of JSON per call, drowns
    # the real narrative). Removed; the dict is static config loaded at
    # import time, so if you need to inspect it, do so at the REPL rather
    # than on every invocation.

    info = config.TF_TYPE_TO_GCLOUD_INFO.get(tf_type)

    if not info:
        log.error("describe_unsupported_type", tf_type=tf_type,
                  reason="no describe command configured")
        return None

    command_args = [config.GCLOUD_CMD_PATH, "--quiet"]
    command_args.extend(info["describe_command"].split())
    
    resource_name_to_pass = mapping["resource_name"]
    if "name_format" in info:
        resource_name_to_pass = info["name_format"].format(name=mapping["resource_name"])
    
    command_args.append(resource_name_to_pass)
    command_args.append(f"--project={mapping['project_id']}")
    
    # Location flag (--zone OR --region) -- C5.1 picker handles
    # zonal-only, regional-only, and dual-mode (GKE) resources via
    # _resolve_location_flag. Previously this was three hardcoded
    # branches that always emitted --zone for any resource declaring
    # zone_flag, which broke regional GKE clusters (location shape
    # "us-central1" rejected by `gcloud container ... describe`).
    command_args.extend(_resolve_location_flag(info, mapping))

    # Parent-identifier flag for nested resources (currently node_pool's
    # --cluster). Kept generic so future nested types can opt in by
    # declaring `cluster_flag` (or an analogous parent_flag) in
    # TF_TYPE_TO_GCLOUD_INFO and stuffing the parent name onto the
    # mapping in run.py _map_asset_to_terraform (C5 wiring).
    if "cluster_flag" in info and "cluster" in mapping:
        command_args.extend([info["cluster_flag"], mapping["cluster"]])

    command_args.append("--format=json")
    
    json_output = shell_runner.run_command(command_args)
    if json_output:
        log.info("describe_complete", tf_type=tf_type,
                 resource_name=mapping.get("resource_name"))
        # Inject `project` as a top-level snapshot field. `gcloud describe`
        # never emits it (the project is implicit in the URL/selfLink path),
        # so without this:
        #   - the LLM has no project value to write into HCL,
        #   - lifecycle_planner.derive_lifecycle_ignores skips `project`
        #     entirely (it only ignores fields PRESENT in the snapshot),
        # and the generated HCL ends up missing `project = "..."`. That's a
        # latent gap: any drop→recreate flow (or `terraform apply` from a
        # fresh shell with no $env:GOOGLE_PROJECT) then fails with
        # `Error: project: required field is not set`.
        #
        # By injecting here we keep the rest of the pipeline unchanged —
        # the CRITICAL OVERRIDE in hcl_generator.py already instructs the
        # LLM to write any optional+computed field with its snapshot value
        # AND add it to lifecycle.ignore_changes. So the file becomes
        # self-contained and future-resilient in one shot.
        try:
            data = json.loads(json_output)
            mutated = False
            if isinstance(data, dict):
                if "project" not in data:
                    data["project"] = mapping["project_id"]
                    log.info("snapshot_inject_project",
                             project_id=mapping["project_id"],
                             reason="gcloud describe omits it; needed for self-contained HCL")
                    mutated = True

                # SA-specific: Terraform's google_service_account requires
                # `account_id` (the local part of the email). schema_oracle
                # correctly classifies `email` as computed (Terraform
                # synthesises it from account_id + project), so the
                # snapshot scrubber strips `email` before the LLM sees it.
                # That destroys the only signal for `account_id`, and the
                # LLM falls back to the HCL block label — which we made
                # HCL-safe by replacing hyphens with underscores — producing
                # illegal account_id values like "poc_sa" that violate
                # GCP's `^[a-z]([-a-z0-9]*[a-z0-9])?$` regex.
                #
                # Inject account_id explicitly here from the email's local
                # part. Deterministic transform (no LLM hallucination
                # risk), runs before the snapshot is scrubbed downstream.
                if mapping.get("tf_type") == "google_service_account":
                    email_value = data.get("email")
                    if email_value and "account_id" not in data:
                        data["account_id"] = email_value.split("@", 1)[0]
                        log.info("snapshot_inject_account_id",
                                 account_id=data["account_id"],
                                 reason="derived from email; email gets scrubbed downstream")
                        mutated = True

            if mutated:
                json_output = json.dumps(data, indent=2)
        except (json.JSONDecodeError, TypeError):
            # Fail-open: keep raw output if we can't parse. Downstream
            # snapshot scrubbers will hit the same error and surface it.
            pass
    return json_output