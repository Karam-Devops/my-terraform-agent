# my-terraform-agent/translator/tf_validator.py

import os
import tempfile
import subprocess
from . import config
from . import shell_runner

def validate_aws_hcl(hcl_content):
    """
    Creates a temporary directory, writes the HCL, initializes the AWS provider,
    and runs 'terraform validate' to mathematically prove syntactic correctness.
    """
    print("\n🔍 [Pillar 1 Proof] Validating AWS HCL Syntax and Schema...")
    
    # We must explicitly add the AWS provider block so `terraform init` knows what to download
    provider_block = """
terraform {
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}
provider "aws" {
  region = "us-east-1" # Dummy region just for validation
}
"""
    full_content = provider_block + "\n" + hcl_content

    # Create a temporary directory to isolate this validation from any other state
    with tempfile.TemporaryDirectory() as temp_dir:
        file_path = os.path.join(temp_dir, "main.tf")
        
        try:
            with open(file_path, "w", encoding="utf-8") as f:
                f.write(full_content)
                
            # 1. Run terraform init (downloads the AWS provider schema)
            print("   - Initializing AWS provider schema (this may take a few seconds)...")
            init_cmd = [config.TERRAFORM_PATH, "init", "-backend=false"]
            # We use subprocess directly here to easily set the 'cwd' (current working directory)
            subprocess.run(init_cmd, cwd=temp_dir, check=True, capture_output=True)
            
            # 2. Run terraform validate
            print("   - Running strict syntax and schema validation...")
            val_cmd = [config.TERRAFORM_PATH, "validate", "-no-color"]
            val_process = subprocess.run(val_cmd, cwd=temp_dir, capture_output=True, text=True)
            
            if val_process.returncode == 0:
                print("   ✅ Validation Successful: The generated code is syntactically perfect AWS HCL.")
                return True, "Success"
            else:
                print("   ❌ Validation Failed: The LLM generated invalid AWS schema/syntax.")
                return False, val_process.stderr

        except subprocess.CalledProcessError as e:
            print(f"   ❌ Critical error running Terraform in validation environment: {e}")
            return False, str(e)
        except Exception as e:
             print(f"   ❌ Unexpected error during validation: {e}")
             return False, str(e)