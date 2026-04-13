# importer/terraform_client.py

import subprocess

from . import config
from . import shell_runner

def init(upgrade=False):
    """Runs 'terraform init'."""
    if upgrade:
        print("\n--- Re-initializing Terraform to update dependencies ---")
        command_args = (config.TERRAFORM_PATH, "init", "-upgrade")
    else:
        # This is now only called once at the start if needed.
        print("Terraform directory not found. Running 'terraform init'...")
        command_args = (config.TERRAFORM_PATH, "init")
    return shell_runner.run_command(command_args)

def import_resource(mapping):
    """
    Runs 'terraform import' for a given resource. This is now a simple wrapper.
    """
    print(f"\n--- Importing '{mapping['resource_name']}' ---")
    tf_address = f'{mapping["tf_type"]}.{mapping["hcl_name"]}'
    import_args = (config.TERRAFORM_PATH, "import", tf_address, mapping["import_id"])
    
    if shell_runner.run_command(import_args) is not None:
        print(f"✅ Import successful for '{mapping['resource_name']}'.")
        return True
    
    print(f"❌ Terraform import failed for '{mapping['resource_name']}'.")
    return False

def plan_for_resource(filename):
    """
    Runs 'terraform plan' targeting a specific file and returns a structured result.
    This is safer for parallel operations than a global plan.
    """
    print(f"\n--- Verifying '{filename}' with 'terraform plan' ---")
    # Using -target is a more precise way to check a single new resource
    # However, a global plan is better to catch cross-resource issues.
    # We will stick to a global plan for now but acknowledge this for future improvement.
    plan_args = (config.TERRAFORM_PATH, "plan", "-no-color", "-input=false")
    
    try:
        plan_output = shell_runner.run_command(plan_args, capture=True)
        if "No changes. Your infrastructure matches the configuration." in plan_output:
            return (True, "Plan successful: No changes.")
        else:
            # This captures cases where the plan is valid but shows a diff
            return (False, plan_output)
    except subprocess.CalledProcessError as e:
        # This captures actual syntax or schema errors where the plan command fails
        return (False, e.stderr)
    except Exception as e:
        # Catch any other unexpected errors
        return (False, str(e))