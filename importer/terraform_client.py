# importer/terraform_client.py

from . import config
from . import shell_runner

def init(upgrade=False):
    """Runs 'terraform init'."""
    if upgrade:
        print("\n--- Re-initializing Terraform to update dependencies ---")
        command_args = (config.TERRAFORM_PATH, "init", "-upgrade")
    else:
        print("Terraform directory not found. Running 'terraform init'...")
        command_args = (config.TERRAFORM_PATH, "init")
    return shell_runner.run_command(command_args)

def import_resource(mapping):
    """Re-initializes Terraform and runs 'terraform import'."""
    if init(upgrade=True) is None:
        print("❌ Terraform re-initialization failed. Aborting import.")
        return False
        
    print("\n--- Importing Resource State ---")
    tf_address = f'{mapping["tf_type"]}.{mapping["hcl_name"]}'
    import_args = (config.TERRAFORM_PATH, "import", tf_address, mapping["import_id"])
    
    if shell_runner.run_command(import_args) is not None:
        print("✅ Import successful.")
        return True
    
    print("❌ Terraform import failed.")
    return False

# --- THIS FUNCTION HAS THE CHANGE ---
def plan():
    """
    Runs 'terraform plan' and returns a tuple: (is_success, output_or_error_string).
    """
    print("\n--- Verifying Configuration with 'terraform plan' ---")
    plan_args = (config.TERRAFORM_PATH, "plan", "-no-color", "-input=false")
    
    # We need to handle success and failure differently to capture the correct output stream
    try:
        # A successful plan will return stdout
        plan_output = shell_runner.run_command(plan_args)
        if "No changes. Your infrastructure matches the configuration." in plan_output:
            print("\n🎉 SUCCESS! The generated HCL perfectly matches the imported resource.")
            return (True, plan_output)
        else:
            print("\n⚠️ VERIFICATION COMPLETE: 'terraform plan' detected differences.")
            return (False, plan_output)
            
    except Exception as e: # run_command will raise an exception on non-zero exit code
        # In case of an error (like a syntax issue), the error message is in the exception
        # We need to modify shell_runner slightly to pass this back. Let's assume the error is in e.stderr for now.
        # A better approach would be to have run_command return a result object.
        # For now, we'll assume the error is captured in the exception string.
        error_output = str(e)
        print("\n❌ VERIFICATION FAILED. The 'terraform plan' command encountered an error.")
        return (False, error_output)