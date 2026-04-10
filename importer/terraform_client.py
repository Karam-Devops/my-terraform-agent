# importer/terraform_client.py
from . import config
from . import shell_runner

def init():
    print("Terraform directory not found. Running 'terraform init'...")
    return shell_runner.run_command((config.TERRAFORM_PATH, "init"))

def import_resource(mapping):
    print("\n--- Importing Resource State ---")
    tf_address = f'{mapping["tf_type"]}.{mapping["hcl_name"]}'
    import_args = (config.TERRAFORM_PATH, "import", tf_address, mapping["import_id"])
    if shell_runner.run_command(import_args) is not None:
        print("✅ Import successful."); return True
    print("❌ Terraform import failed."); return False

def plan():
    print("\n--- Verifying Configuration with 'terraform plan' ---")
    plan_args = (config.TERRAFORM_PATH, "plan", "-no-color")
    plan_output = shell_runner.run_command(plan_args)
    if plan_output is None:
        print("❌ Terraform plan failed.")
    elif "No changes. Your infrastructure matches the configuration." in plan_output:
        print("\n🎉 SUCCESS! The generated HCL perfectly matches the imported resource.")
    else:
        print("\n⚠️ VERIFICATION COMPLETE: 'terraform plan' detected differences.")
        print(f"   Review the plan output or the file '{config.TERRAFORM_PATH} plan' for details.")