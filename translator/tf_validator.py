# my-terraform-agent/translator/tf_validator.py

import os
import tempfile
import subprocess
from . import config
from . import shell_runner

def validate_hcl(hcl_content, target_cloud):
    """
    Validates HCL syntax for the specified target cloud provider.
    """
    print(f"\n🔍 [Pillar 1 Proof] Validating {target_cloud.upper()} HCL Syntax and Schema...")
    
    if target_cloud == "aws":
        provider_block = """
terraform {
  required_providers {
    aws = { source = "hashicorp/aws", version = "~> 5.0" }
  }
}
provider "aws" { region = "us-east-1" }
"""
    elif target_cloud == "azure":
        provider_block = """
terraform {
  required_providers {
    azurerm = { source = "hashicorp/azurerm", version = "~> 3.0" }
  }
}
provider "azurerm" {
  features {}
  skip_provider_registration = true
}
"""
    else:
        return False, "Unknown target cloud."

    full_content = provider_block + "\n" + hcl_content

    with tempfile.TemporaryDirectory() as temp_dir:
        file_path = os.path.join(temp_dir, "main.tf")
        
        try:
            with open(file_path, "w", encoding="utf-8") as f:
                f.write(full_content)
                
            print(f"   - Initializing {target_cloud.upper()} provider schema...")
            init_cmd = [config.TERRAFORM_PATH, "init", "-backend=false"]
            subprocess.run(init_cmd, cwd=temp_dir, check=True, capture_output=True)
            
            print("   - Running strict syntax and schema validation...")
            val_cmd = [config.TERRAFORM_PATH, "validate", "-no-color"]
            val_process = subprocess.run(val_cmd, cwd=temp_dir, capture_output=True, text=True)
            
            if val_process.returncode == 0:
                print("   ✅ Validation Successful: The generated code is syntactically perfect.")
                return True, "Success"
            else:
                print("   ❌ Validation Failed: The LLM generated invalid schema/syntax.")
                return False, val_process.stderr

        except subprocess.CalledProcessError as e:
            print(f"   ❌ Critical error running Terraform: {e}")
            return False, str(e)
        except Exception as e:
             return False, str(e)