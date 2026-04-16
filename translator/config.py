# my-terraform-agent/translator/config.py

# --- Paths ---
TERRAFORM_PATH = r"C:\Terraform\terraform.exe"

# --- LLM Settings ---
MAX_RETRIES = 3 # We'll keep retries lower for translation, as it's less prone to state-engine quirks

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
"""