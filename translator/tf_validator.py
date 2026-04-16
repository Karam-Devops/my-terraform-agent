# my-terraform-agent/translator/tf_validator.py

import os
import tempfile
import subprocess
import logging
from typing import Tuple
from . import config

# Initialize standard logger for enterprise observability
logger = logging.getLogger(__name__)

def validate_hcl(hcl_content: str, target_cloud: str) -> Tuple[bool, str]:
    """
    Validates HCL syntax for the specified target cloud provider by running 
    an isolated 'terraform init' and 'terraform validate'.
    
    Returns:
        Tuple[bool, str]: (is_valid, error_message_or_success)
    """
    logger.info(f"🔍 [Pillar 1 Proof] Validating {target_cloud.upper()} HCL Syntax and Schema...")
    
    target = target_cloud.lower()
    if target == "aws":
        provider_block = """
terraform {
  required_providers {
    aws = { source = "hashicorp/aws", version = "~> 5.0" }
  }
}
provider "aws" { region = "us-east-1" }
"""
    elif target == "azure":
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
        return False, f"Unknown target cloud: {target_cloud}"

    # Combine the mock provider with the generated code
    full_content = provider_block + "\n" + hcl_content

    # Run in an ephemeral directory to prevent state pollution
    with tempfile.TemporaryDirectory() as temp_dir:
        file_path = os.path.join(temp_dir, "main.tf")
        
        try:
            with open(file_path, "w", encoding="utf-8") as f:
                f.write(full_content)
                
            logger.info(f"   - Initializing {target_cloud.upper()} provider schema...")
            init_cmd = [config.TERRAFORM_PATH, "init", "-backend=false"]
            
            # Run init (throws CalledProcessError if it fails)
            subprocess.run(init_cmd, cwd=temp_dir, check=True, capture_output=True, text=True)
            
            logger.info("   - Running strict syntax and schema validation...")
            val_cmd = [config.TERRAFORM_PATH, "validate", "-no-color"]
            
            # Run validate (does not throw on failure, we check returncode manually)
            val_process = subprocess.run(val_cmd, cwd=temp_dir, capture_output=True, text=True)
            
            if val_process.returncode == 0:
                logger.info("   ✅ Validation Successful: The generated code is syntactically perfect.")
                return True, "Success"
            else:
                logger.warning("   ❌ Validation Failed: The LLM generated invalid schema/syntax.")
                # Return the stderr/stdout so it can be fed back to LangGraph/LLM for self-correction
                error_output = val_process.stderr.strip() or val_process.stdout.strip()
                return False, error_output

        except subprocess.CalledProcessError as e:
            # Captures failures during `terraform init` (e.g., network issues, bad provider block)
            logger.error(f"   ❌ Critical error running Terraform Init: {e.stderr}")
            return False, f"Terraform Init Failed: {e.stderr}"
        except Exception as e:
            # Captures unexpected OS/Python errors
            logger.exception(f"   ❌ Unexpected System Error during validation: {e}")
            return False, str(e)