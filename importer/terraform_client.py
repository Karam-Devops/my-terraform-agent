# my-terraform-agent/importer/terraform_client.py

import subprocess
import os
import tempfile
from . import config

def init(upgrade=False):
    """Runs 'terraform init'."""
    print(f"\n--- {'Re-initializing' if upgrade else 'Initializing'} Terraform ---")
    command_args = [config.TERRAFORM_PATH, "init"]
    if upgrade:
        command_args.append("-upgrade")
    try:
        # Using a simple run for this non-critical command
        subprocess.run(command_args, check=True, capture_output=True)
        return True
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        print(f"Terraform init failed: {e}")
        return False

def import_resource(mapping):
    """Runs 'terraform import' for a given resource."""
    print(f"\n--- Importing '{mapping['resource_name']}' ---")
    tf_address = f'{mapping["tf_type"]}.{mapping["hcl_name"]}'
    import_args = [config.TERRAFORM_PATH, "import", tf_address, mapping["import_id"]]
    try:
        subprocess.run(import_args, check=True, capture_output=True, text=True)
        print(f"✅ Import successful for '{mapping['resource_name']}'.")
        return True
    except subprocess.CalledProcessError as e:
        error_output = e.stderr or e.stdout
        print(f"❌ Terraform import failed for '{mapping['resource_name']}'. Error: {error_output.splitlines()[0]}")
        return False

def plan_for_resource(filename):
    """
    Runs 'terraform plan', redirecting all output to a temp file to guarantee capture.
    """
    print(f"\n--- Verifying '{filename}' with 'terraform plan' ---")
    plan_args = [config.TERRAFORM_PATH, "plan", "-no-color", "-input=false"]

    # --- THIS IS THE DEFINITIVE FIX: REDIRECT TO FILE ---
    output = ""
    try:
        # 1. Create a temporary file to capture all output.
        with tempfile.NamedTemporaryFile(mode='w+', delete=False, encoding='utf-8') as temp_f:
            temp_filename = temp_f.name
            print(f"   - Capturing output to temporary file: {temp_filename}")
        
        # 2. Run the command, redirecting both stdout and stderr to the temp file.
        with open(temp_filename, 'w', encoding='utf-8') as f_out:
            process = subprocess.run(
                plan_args,
                stdout=f_out,
                stderr=f_out
            )
        
        # 3. Read the entire content of the file, which now contains the full output.
        with open(temp_filename, 'r', encoding='utf-8') as f_in:
            output = f_in.read()

        # 4. Check the return code to see if the command succeeded or failed.
        if process.returncode == 0 and "No changes. Your infrastructure matches the configuration." in output:
            print("   - Plan successful: No changes.")
            return (True, "Plan successful: No changes.")
        else:
            # If the return code is 0 but there's a diff, it's a failure for our goal.
            # If the return code is non-zero, it's a hard failure. In both cases, the output is the error/diff.
            print("   - Plan command indicated changes or failed. Capturing full output from file.")
            return (False, output)

    finally:
        # 5. Clean up by deleting the temporary file.
        if os.path.exists(temp_filename):
            os.remove(temp_filename)
    # --- END OF DEFINITIVE FIX ---