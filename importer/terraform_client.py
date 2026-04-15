# my-terraform-agent/importer/terraform_client.py

import subprocess
import os
import tempfile
from . import config

def init(upgrade=False):
    print(f"\n--- {'Re-initializing' if upgrade else 'Initializing'} Terraform ---")
    command_args = [config.TERRAFORM_PATH, "init"]
    if upgrade: command_args.append("-upgrade")
    try:
        subprocess.run(command_args, check=True, capture_output=True)
        return True
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        print(f"Terraform init failed: {e}")
        return False

def import_resource(mapping, force_refresh=False):
    """
    Runs 'terraform import'.
    If force_refresh is True, it explicitly removes the resource from the state
    first to ensure a clean mapping with the newly generated HCL.
    """
    tf_address = f'{mapping["tf_type"]}.{mapping["hcl_name"]}'
    
    # --- NEW LOGIC: Forcefully clean the state if requested ---
    if force_refresh:
        print(f"\n   - 🧹 Forcing state refresh for '{mapping['resource_name']}'...")
        remove_args = [config.TERRAFORM_PATH, "state", "rm", tf_address]
        try:
            # We don't care if this fails (e.g., if it wasn't in the state to begin with)
            subprocess.run(remove_args, capture_output=True, text=True)
        except Exception:
            pass
    # --- END OF NEW LOGIC ---

    print(f"\n--- Importing '{mapping['resource_name']}' ---")
    import_args = [config.TERRAFORM_PATH, "import", tf_address, mapping["import_id"]]
    try:
        subprocess.run(import_args, check=True, capture_output=True, text=True)
        print(f"✅ Import successful for '{mapping['resource_name']}'.")
        return True
    except subprocess.CalledProcessError as e:
        error_output = e.stderr or e.stdout
        
        # We still want to handle the "already managed" case gracefully for the initial run
        if "Resource already managed by Terraform" in error_output and not force_refresh:
            print(f"✅ Resource '{mapping['resource_name']}' is already managed in state. Skipping import.")
            return True
            
        print(f"❌ Terraform import failed for '{mapping['resource_name']}'. Error: {error_output.splitlines()[0]}")
        return False

def plan_for_resource(filename):
    print(f"\n--- Verifying '{filename}' with 'terraform plan' ---")
    plan_args = [config.TERRAFORM_PATH, "plan", "-no-color", "-input=false"]

    output = ""
    try:
        with tempfile.NamedTemporaryFile(mode='w+', delete=False, encoding='utf-8') as temp_f:
            temp_filename = temp_f.name
        
        with open(temp_filename, 'w', encoding='utf-8') as f_out:
            process = subprocess.run(plan_args, stdout=f_out, stderr=f_out)
        
        with open(temp_filename, 'r', encoding='utf-8') as f_in:
            output = f_in.read()

        if process.returncode == 0 and "No changes. Your infrastructure matches the configuration." in output:
            print("   - Plan successful: No changes.")
            return (True, "Plan successful: No changes.")
        else:
            print("   - Plan command indicated changes or failed.")
            return (False, output)
    finally:
        if os.path.exists(temp_filename):
            os.remove(temp_filename)