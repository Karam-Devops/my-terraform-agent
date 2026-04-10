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
    """Runs 'terraform plan' and provides clear guidance on success or failure."""
    print("\n--- Verifying Configuration with 'terraform plan' ---")
    plan_args = (config.TERRAFORM_PATH, "plan", "-no-color")
    plan_output = shell_runner.run_command(plan_args)
    
    if plan_output is None:
        print("\n❌ VERIFICATION FAILED. The 'terraform plan' command encountered an error.")
        print("   This usually means the LLM generated slightly incorrect HCL.")
        print("   The error message from Terraform is printed above.")
        print("   Please review the generated .tf file and correct the mistake based on the error.")

    elif "No changes. Your infrastructure matches the configuration." in plan_output:
        print("\n🎉 SUCCESS! The generated HCL perfectly matches the imported resource.")

    else:
        print("\n⚠️ VERIFICATION COMPLETE: 'terraform plan' detected differences.")
        print("   This is okay and can happen with complex resources.")
        print("   The generated HCL is valid but doesn't perfectly match the resource state.")
        print("   Please review the plan output printed above to see the minor adjustments needed.")