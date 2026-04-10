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

    command_args.append("--format=json")
    
    json_output = shell_runner.run_command(command_args)
    if json_output:
        print("✅ Successfully retrieved full resource configuration.")
    return json_output