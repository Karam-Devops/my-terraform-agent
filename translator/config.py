# my-terraform-agent/translator/config.py

from common.terraform_path import resolve_terraform_path as _resolve_terraform

# --- Paths ---
# TERRAFORM_PATH is resolved lazily on first attribute access (see __getattr__
# at the bottom of this module). See common/terraform_path.py for the
# resolution order ($TERRAFORM_BINARY → platform default → PATH → fail).

# --- LLM Settings ---
MAX_RETRIES = 3 # We'll keep retries lower for translation, as it's less prone to state-engine quirks

# --- Concurrency (P4-15) ---
# Bound on parallel translation workers when run_translation_batch fans
# out across multiple source files. Each in-flight worker holds 3
# Vertex AI requests in sequence (blueprint extract -> target HCL gen
# -> validator-driven re-prompts), so the per-minute LLM-call rate is
# roughly workers * 3 * (1 / per-file-duration-seconds). Default 4
# leaves comfortable headroom under Vertex AI's typical 60 RPM Gemini
# Pro per-project quota (4 workers * 3 calls / ~30s = ~24 RPM avg).
#
# Override via env var if you've raised the project quota OR want
# stricter throttling on a smoke run:
#     export MAX_TRANSLATION_WORKERS=8   # higher -- needs quota headroom
#     export MAX_TRANSLATION_WORKERS=1   # serialize for predictable retros
#
# Setting to 1 is the safest fallback for any environment where the
# Vertex AI quota is unknown -- the batch still runs, just sequentially.
import os as _os
MAX_TRANSLATION_WORKERS = int(_os.environ.get("MAX_TRANSLATION_WORKERS", "4"))

# --- Translation Heuristics (The "Rosetta Stone") ---
AWS_ARCHITECTURAL_RULES = """
CRITICAL AWS ARCHITECTURAL RULES:
1.  **Strict Resource Naming:** You MUST use the exact, official HashiCorp AWS provider resource names. Do NOT invent logical aliases.
    *   Virtual Machines must be exactly: `aws_instance` (NOT `aws_ec2_instance`).
    *   Disks must be exactly: `aws_ebs_volume`.
    *   Firewalls must be exactly: `aws_security_group` or `aws_network_acl`.
    *   Networks must be exactly: `aws_vpc` and `aws_subnet`.
2.  **Identity (IAM):** If the blueprint specifies a 'service_account' or 'identity', you MUST NOT create an 'aws_iam_user' or access keys. You MUST create an `aws_iam_role`, an `aws_iam_role_policy` (or attachment), and an `aws_iam_instance_profile`. The EC2 instance must reference the instance profile.
3.  **Networking (VPC):** AWS EC2 instances require a `subnet_id`. If the blueprint does not provide a specific subnet ID (because it was translating from a global GCP network), you MUST use a variable (e.g., `var.subnet_id`) or a `data "aws_subnet"` block. Do not hardcode a fake subnet ID.
4.  **Security (Firewalls):** GCP firewalls are often global or network-wide. AWS uses Security Groups attached directly to the instance (or ENI). If the blueprint implies network access (e.g., tags for HTTP/HTTPS), you MUST generate an `aws_security_group` and attach its ID to the `vpc_security_group_ids` argument of the EC2 instance.
5.  **Sizing (Instances):** Translate generic sizes intelligently. E.g., 'small' -> 't3.micro', 'medium' -> 't3.medium' or 'm5.large', 'large' -> 'm5.xlarge'.
6.  **Storage:** Map standard disks to `gp3` volume types.
7.  **Advanced VM Features (vTPM/Shielded):** Do NOT attempt to map GCP Shielded VM features (like `enable_vtpm` or `enable_secure_boot`) directly to top-level arguments like `tpm_support` on standard `aws_instance` resources. These require complex Nitro Enclave setups or specific AMI configurations in AWS. OMIT these advanced security features from the generated HCL and instead add a `# TODO:` comment explaining that advanced enclave/TPM support requires manual architecture.
8. **Spot/Preemptible Instances (CRITICAL):** Do NOT use `instance_interruption_behavior` or similar spot/preemptible settings as top-level arguments on `aws_instance`. If translating a preemptible or spot instance, you MUST nest these settings inside an `instance_market_options` block. Map the GCP termination action to the SEMANTICALLY EQUIVALENT AWS interruption behavior (NOT a default):
   *   GCP `instance_termination_action = "STOP"` → AWS `instance_interruption_behavior = "stop"` (preserves the root volume; instance can resume)
   *   GCP `instance_termination_action = "DELETE"` → AWS `instance_interruption_behavior = "terminate"` (destroys the instance and its root volume)
   *   GCP scheduling with hibernation → AWS `instance_interruption_behavior = "hibernate"`
   These are NOT interchangeable. Substituting "terminate" for a source "STOP" silently destroys data the operator expected to preserve.
   Example (for a source STOP):
   instance_market_options {
     market_type = "spot"
     spot_options {
       instance_interruption_behavior = "stop"
     }
   }
"""

AZURE_ARCHITECTURAL_RULES = """
CRITICAL AZURE ARCHITECTURAL RULES:
1.  **Strict Resource Naming:** You MUST use the exact, official HashiCorp AzureRM provider resource names.
    *   Virtual Machines: Use `azurerm_linux_virtual_machine` or `azurerm_windows_virtual_machine`. Do NOT use the deprecated `azurerm_virtual_machine`.
    *   Networks: `azurerm_virtual_network` and `azurerm_subnet`.
    *   Public IPs: `azurerm_public_ip`.
    *   Resource Groups: Every Azure resource requires a `resource_group_name`. You MUST generate an `azurerm_resource_group` block or use a variable for it.
2.  **Identity:** Translate service accounts into an Azure Managed Identity by adding an `identity { type = "SystemAssigned" }` block inside the virtual machine resource.
3.  **Networking:** Azure VMs require a Network Interface. You MUST generate an `azurerm_network_interface` resource and attach it to the VM using `network_interface_ids`.
4.  **Public IP Mapping (CRITICAL):** If translating a GCP instance with an external IP, create an `azurerm_public_ip`.
    *   **SKU Mapping:** Azure Public IP `sku` ONLY accepts "Basic" or "Standard". Map "Premium" to "Standard".
5.  **Storage:** Azure VMs require an `os_disk` block (e.g., caching = "ReadWrite", storage_account_type = "Standard_LRS").
6.  **Sizing:** E.g., 'small' -> 'Standard_B1s', 'medium' -> 'Standard_D2s_v3'.
7.  **Authentication:** Linux VMs require `admin_username` and an `admin_ssh_key` block. Do NOT hardcode fake SSH keys. You MUST use a variable reference (e.g., `var.admin_ssh_key`). 
    *   **CRITICAL REQUIREMENT:** If you use a variable, you MUST also generate the corresponding `variable "admin_ssh_key" { type = string }` declaration block in your output.
8.  **Advanced VM Features:** If the blueprint specifies vTPM or Secure Boot, use the top-level boolean arguments `secure_boot_enabled = true` and/or `vtpm_enabled = true` inside the `azurerm_linux_virtual_machine` block. Do NOT use `security_type`.
9.  **Availability Zones (CRITICAL INCONSISTENCY):** You must pay close attention to the resource type when assigning zones:
    *   For `azurerm_linux_virtual_machine` or `azurerm_windows_virtual_machine`: You MUST use the singular `zone` argument with a string value (e.g., `zone = "1"`).
    *   For `azurerm_public_ip`: You MUST use the plural `zones` argument with a list of strings (e.g., `zones = ["1"]`).
10. **Variable declaration completeness (STRICT):** For EVERY `var.<NAME>` reference you emit anywhere in the HCL output, you MUST also emit a corresponding `variable "<NAME>" { ... }` declaration block in the same output. The output is a self-contained Terraform module — undeclared variables cause `terraform validate` to fail with `Reference to undeclared input variable`. This rule subsumes Rule 7's `admin_ssh_key` requirement and applies to every other variable reference (e.g., `var.resource_group_name`, `var.location`, `var.subnet_id`). Before finalizing your output, scan it: every `var.X` you wrote needs a `variable "X" {}` block somewhere in the same file. References inside `#` comments or `/* ... */` blocks do not need declarations.
"""


# --- Lazy attributes (PEP 562) ---
# Existing callers do `config.TERRAFORM_PATH`. Defer resolution to first
# access so this module imports cleanly even when Terraform isn't yet on
# the machine, and so a deployment-time TERRAFORM_BINARY env var still
# wins over any import-time snapshot.
def __getattr__(name):
    if name == "TERRAFORM_PATH":
        return _resolve_terraform()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")