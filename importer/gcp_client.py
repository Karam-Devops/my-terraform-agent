# importer/gcp_client.py
import json
from . import config
from . import shell_runner

def discover_resources_of_type(project_id, asset_type):
    print(f"\n🔎 Searching for asset type: {asset_type}...")
    command_args = (
        config.GCLOUD_CMD_PATH, "--quiet", "asset", "search-all-resources",
        f"--scope=projects/{project_id}", f"--asset-types={asset_type}", "--format=json"
    )
    output = shell_runner.run_command(command_args)
    if not output: return []
    try:
        resources = json.loads(output)
        print(f"   ✅ Found {len(resources)} resource(s).")
        return resources
    except json.JSONDecodeError:
        print("   ❌ Error: Failed to parse JSON response from gcloud.")
        return []

def get_resource_details_json(mapping):
    """Gets the full JSON configuration for a selected resource using 'gcloud describe'."""
    print("\n--- Getting Full Resource Details ---")
    
    tf_type = mapping["tf_type"]
    print(f"   - Attempting to find describe command for Terraform type: '{tf_type}'")
    
    # ---------------------------------------------------------------------------------
    # THIS IS THE CRITICAL DEBUGGING LINE
    # ---------------------------------------------------------------------------------
    print("\n   [DEBUG] Contents of the loaded TF_TYPE_TO_GCLOUD_INFO dictionary:")
    print(f"   {json.dumps(config.TF_TYPE_TO_GCLOUD_INFO, indent=4)}")
    # ---------------------------------------------------------------------------------
    
    info = config.TF_TYPE_TO_GCLOUD_INFO.get(tf_type)
    
    if not info:
        print(f"\n❌ Cannot get details: No 'describe' command configured for {tf_type}.")
        return None

    command_args = [config.GCLOUD_CMD_PATH, "--quiet"]
    command_args.extend(info["describe_command"].split())
    
    resource_name_to_pass = mapping["resource_name"]
    if "name_format" in info:
        resource_name_to_pass = info["name_format"].format(name=mapping["resource_name"])
    
    command_args.append(resource_name_to_pass)
    command_args.append(f"--project={mapping['project_id']}")
    
    if "zone_flag" in info and "location" in mapping:
        command_args.extend([info["zone_flag"], mapping["location"]])
    # Symmetric branch for regional resources (compute_subnetwork,
    # compute_address). Without this, gcloud rejects the describe call
    # with "Underspecified resource — please specify --region". The
    # config dict already declares region_flag for these types; this
    # branch wires it through. NOTE: a `cluster_flag` branch is still
    # missing — google_container_node_pool will hit the same bug class
    # when it's first imported. Fix together when node_pool is added.
    if "region_flag" in info and "location" in mapping:
        command_args.extend([info["region_flag"], mapping["location"]])

    command_args.append("--format=json")
    
    json_output = shell_runner.run_command(command_args)
    if json_output:
        print("✅ Successfully retrieved full resource configuration.")
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
                    print(f"   - 🏷️  Injected project='{mapping['project_id']}' into snapshot "
                          f"(gcloud describe omits it; needed for self-contained HCL).")
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
                        print(f"   - 🏷️  Injected account_id='{data['account_id']}' for SA "
                              f"(derived from email; email gets scrubbed downstream).")
                        mutated = True

            if mutated:
                json_output = json.dumps(data, indent=2)
        except (json.JSONDecodeError, TypeError):
            # Fail-open: keep raw output if we can't parse. Downstream
            # snapshot scrubbers will hit the same error and surface it.
            pass
    return json_output