# my-terraform-agent/importer/terraform_client.py

import subprocess
import os
import tempfile
from . import config

def _ensure_initialized():
    """Internal helper: Checks if Terraform is initialized; runs init if not."""
    if not os.path.isdir(".terraform") or not os.path.isfile(".terraform.lock.hcl"):
        print("   - ⚠️ Terraform plugins missing or lock file inconsistent. Auto-initializing...")
        # Force an upgrade to ensure the lock file is written correctly for the current .tf files
        return init(upgrade=True)
    return True

def init(upgrade=False):
    """Runs 'terraform init'."""
    print(f"\n--- {'Re-initializing' if upgrade else 'Initializing'} Terraform ---")
    command_args = [config.TERRAFORM_PATH, "init"]
    if upgrade:
        command_args.append("-upgrade")
    try:
        # Use subprocess.run directly as we don't need the complex file-redirection here
        subprocess.run(command_args, check=True, capture_output=True, text=True)
        return True
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        error_output = e.stderr if hasattr(e, 'stderr') and e.stderr else str(e)
        print(f"❌ Terraform init failed. Error: {error_output}")
        return False

def import_resource(mapping, force_refresh=False):
    """Runs 'terraform import', ensuring initialization first."""
    if not _ensure_initialized():
        print(f"❌ Aborting import for '{mapping['resource_name']}' due to initialization failure.")
        return False

    tf_address = f'{mapping["tf_type"]}.{mapping["hcl_name"]}'
    
    if force_refresh:
        print(f"\n   - 🧹 Forcing state refresh for '{mapping['resource_name']}'...")
        remove_args = [config.TERRAFORM_PATH, "state", "rm", tf_address]
        try:
            subprocess.run(remove_args, capture_output=True, text=True)
        except Exception:
            pass

    print(f"\n--- Importing '{mapping['resource_name']}' ---")
    import_args = [config.TERRAFORM_PATH, "import", tf_address, mapping["import_id"]]
    try:
        subprocess.run(import_args, check=True, capture_output=True, text=True)
        print(f"✅ Import successful for '{mapping['resource_name']}'.")
        return True
    except subprocess.CalledProcessError as e:
        error_output = e.stderr if e.stderr else e.stdout
        
        if "Resource already managed by Terraform" in error_output and not force_refresh:
            print(f"✅ Resource '{mapping['resource_name']}' is already managed in state. Skipping import.")
            return True
            
        # Extract just the first line for cleaner logging
        first_line = error_output.splitlines()[0] if error_output else "Unknown Error"
        print(f"❌ Terraform import failed for '{mapping['resource_name']}'. Error: {first_line}")
        return False

def plan_for_resource(filename):
    """Runs 'terraform plan', ensuring initialization and robustly capturing output."""
    if not _ensure_initialized():
        return (False, "CRITICAL: Terraform failed to initialize. Cannot run plan.")

    print(f"\n--- Verifying '{filename}' with 'terraform plan' ---")
    plan_args = [config.TERRAFORM_PATH, "plan", "-no-color", "-input=false"]

    output = ""
    try:
        with tempfile.NamedTemporaryFile(mode='w+', delete=False, encoding='utf-8') as temp_f:
            temp_filename = temp_f.name
        
        with open(temp_filename, 'w', encoding='utf-8') as f_out:
            process = subprocess.run(
                plan_args,
                stdout=f_out,
                stderr=f_out
            )
        
        with open(temp_filename, 'r', encoding='utf-8') as f_in:
            output = f_in.read()

        if process.returncode == 0 and "No changes. Your infrastructure matches the configuration." in output:
            print("   - Plan successful: No changes.")
            return (True, "Plan successful: No changes.")
        else:
            print("   - Plan command indicated changes or failed. Capturing full output from file.")
            return (False, output)

    finally:
        if os.path.exists(temp_filename):
            os.remove(temp_filename)