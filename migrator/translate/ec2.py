"""GCP google_compute_instance → AWS aws_instance.

Source pattern (the customer's vm/compute-instance terragrunt.hcl):

    inputs = {
      labels = merge(...)
      vm_configs = [
        {
          name          = "..."
          instance_type = "e2-highcpu-8"
          network       = "projects/.../networks/vpc-..."
          subnetwork    = "projects/.../subnetworks/sb-..."
          zone          = "northamerica-northeast1-a"
          metadata      = { ... }
          addresses     = { internal = [...] }
          service_account_email = "..."
          boot_disk_image = "windows-cloud/windows-2025"
          boot_disk_size  = 500
          boot_disk_type  = "pd-standard"
          tags            = [...]
        },
      ]
    }
"""

from __future__ import annotations

import re
from typing import Any, Dict, List

from migrator.results import DiscoveredResource

from .base import DEFAULT_VERSIONS_TF, AWSModuleSpec, Translation


SERVICE_NAME = "ec2-instance"


# GCP machine type → AWS instance type. Graviton-preferred (Arm64) where
# parity exists — cheaper + lower power than Intel/AMD equivalents.
# Operator can override per-instance post-emission for x86-only AMIs.
#
# Stays consistent with the EKS translator's _MACHINE_TYPE_MAP so EC2
# nodes and EKS nodes get the same sizing rationale.
_INSTANCE_TYPE_MAP = {
    # ---- e2 series (cost-optimized) → Graviton t4g/m7g ----
    "e2-micro":       "t4g.micro",
    "e2-small":       "t4g.small",
    "e2-medium":      "t4g.medium",
    "e2-standard-2":  "m7g.large",
    "e2-standard-4":  "m7g.xlarge",
    "e2-standard-8":  "m7g.2xlarge",
    "e2-standard-16": "m7g.4xlarge",
    "e2-standard-32": "m7g.8xlarge",
    # ---- e2 highcpu (compute-optimized) → c7g ----
    "e2-highcpu-2":   "c7g.large",
    "e2-highcpu-4":   "c7g.xlarge",
    "e2-highcpu-8":   "c7g.2xlarge",
    "e2-highcpu-16":  "c7g.4xlarge",
    "e2-highcpu-32":  "c7g.8xlarge",
    # ---- e2 highmem (memory-optimized) → r7g ----
    "e2-highmem-2":   "r7g.large",
    "e2-highmem-4":   "r7g.xlarge",
    "e2-highmem-8":   "r7g.2xlarge",
    "e2-highmem-16":  "r7g.4xlarge",
    # ---- n1 series (legacy general purpose) → m7g ----
    "n1-standard-1":  "m7g.medium",
    "n1-standard-2":  "m7g.large",
    "n1-standard-4":  "m7g.xlarge",
    "n1-standard-8":  "m7g.2xlarge",
    "n1-standard-16": "m7g.4xlarge",
    # ---- n2 series → m7i (Intel) for x86 parity ----
    "n2-standard-2":  "m7i.large",
    "n2-standard-4":  "m7i.xlarge",
    "n2-standard-8":  "m7i.2xlarge",
    "n2-standard-16": "m7i.4xlarge",
    "n2-standard-32": "m7i.8xlarge",
    # ---- n2d series (AMD) → m7a ----
    "n2d-standard-2": "m7a.large",
    "n2d-standard-4": "m7a.xlarge",
    "n2d-standard-8": "m7a.2xlarge",
    "n2d-standard-16": "m7a.4xlarge",
    # ---- c2 / c2d (compute-optimized) ----
    "c2-standard-4":  "c7g.xlarge",
    "c2-standard-8":  "c7g.2xlarge",
    "c2-standard-16": "c7g.4xlarge",
    "c2d-standard-4": "c7a.xlarge",
    "c2d-standard-8": "c7a.2xlarge",
    # ---- m1 / m2 (memory-optimized) ----
    "m1-ultramem-40":  "r7g.8xlarge",
    "m1-megamem-96":   "r7g.16xlarge",
    "m2-ultramem-208": "r7g.16xlarge",
}


def _map_instance_type(gcp_type: str) -> str:
    """Map a GCP machine_type to an AWS instance type. Falls back to a
    CPU-count heuristic for unrecognized custom shapes (custom-N-M)."""
    if not gcp_type:
        return "t4g.medium"
    mapped = _INSTANCE_TYPE_MAP.get(gcp_type)
    if mapped:
        return mapped
    # Heuristic for unrecognized custom shapes like "custom-N-M":
    m = re.match(r"custom-(\d+)-(\d+)", gcp_type)
    if m:
        cpus = int(m.group(1))
        memory_mb = int(m.group(2))
        # Memory-to-CPU ratio guides family choice.
        mem_per_cpu = memory_mb / cpus if cpus > 0 else 0
        if mem_per_cpu >= 7000:    # memory-optimized
            if cpus <= 2:  return "r7g.large"
            if cpus <= 4:  return "r7g.xlarge"
            if cpus <= 8:  return "r7g.2xlarge"
            if cpus <= 16: return "r7g.4xlarge"
            return "r7g.8xlarge"
        if mem_per_cpu < 2000:    # compute-optimized
            if cpus <= 2:  return "c7g.large"
            if cpus <= 4:  return "c7g.xlarge"
            if cpus <= 8:  return "c7g.2xlarge"
            if cpus <= 16: return "c7g.4xlarge"
            return "c7g.8xlarge"
        # General-purpose default (Graviton)
        if cpus <= 2:  return "m7g.large"
        if cpus <= 4:  return "m7g.xlarge"
        if cpus <= 8:  return "m7g.2xlarge"
        if cpus <= 16: return "m7g.4xlarge"
        return "m7g.8xlarge"
    return "t4g.medium"   # last-resort default


# GCP boot disk image → AWS AMI lookup hint.
_BOOT_IMAGE_MAP = {
    "debian-cloud/debian-12":      "ami-debian-12 (use data.aws_ami)",
    "debian-cloud/debian-11":      "ami-debian-11 (use data.aws_ami)",
    "ubuntu-os-cloud/ubuntu-2204": "ami-ubuntu-22-04 (use data.aws_ami)",
    "ubuntu-os-cloud/ubuntu-2004": "ami-ubuntu-20-04 (use data.aws_ami)",
    "centos-cloud/centos-7":       "ami-centos-7 (use data.aws_ami)",
    "rhel-cloud/rhel-8":           "ami-rhel-8 (use data.aws_ami)",
    "rhel-cloud/rhel-9":           "ami-rhel-9 (use data.aws_ami)",
    "windows-cloud/windows-2025":  "ami-windows-server-2025 (use data.aws_ami)",
    "windows-cloud/windows-2022":  "ami-windows-server-2022 (use data.aws_ami)",
    "windows-cloud/windows-2019":  "ami-windows-server-2019 (use data.aws_ami)",
}


# GCP boot disk type → AWS EBS type.
_DISK_TYPE_MAP = {
    "pd-standard": "gp3",
    "pd-balanced": "gp3",
    "pd-ssd":      "io2",
    "pd-extreme":  "io2",
}


def translate(resource: DiscoveredResource) -> Translation:
    args = resource.arguments or {}
    notes: List[str] = []

    raw_vms = args.get("vm_configs") or args.get("vms") or args.get("instances") or []
    if not isinstance(raw_vms, list):
        raw_vms = []

    instances = []
    for src in raw_vms:
        if not isinstance(src, dict):
            continue
        name = str(src.get("name", "TODO-instance-name"))
        gcp_type = str(src.get("instance_type", "e2-medium"))
        aws_type = _map_instance_type(gcp_type)
        if gcp_type not in _INSTANCE_TYPE_MAP:
            notes.append(f"VM `{name}`: unmapped GCP machine type `{gcp_type}` → derived `{aws_type}` "
                         f"via CPU/memory heuristic; review for workload fit.")

        gcp_image = str(src.get("boot_disk_image", "debian-cloud/debian-12"))
        ami_hint = _BOOT_IMAGE_MAP.get(gcp_image, f"TODO: lookup AMI for {gcp_image}")

        gcp_disk_type = str(src.get("boot_disk_type", "pd-standard"))
        aws_disk_type = _DISK_TYPE_MAP.get(gcp_disk_type, "gp3")

        boot_disk_size = int(src.get("boot_disk_size", 20) or 20)

        # tags (GCP) → AWS tags; GCP `tags` is a list of strings (network tags),
        # they conceptually map to security-group attachment, NOT to AWS tags.
        # We surface them as a separate list for the operator to wire.
        gcp_network_tags = src.get("tags") or []
        if not isinstance(gcp_network_tags, list):
            gcp_network_tags = []

        # service account email → IAM instance profile name.
        # When the source's `service_account_email` is itself an
        # unresolvable interpolation (dependency.X / var.X / each.X /
        # ${local...}), we can't derive a useful profile name from it
        # — constructing "${...}-instance-profile" yields a literal
        # TODO string that breaks at apply time (the IAM profile
        # doesn't exist). Skip and leave the line commented out;
        # operator wires manually or via a var. Kiro v8 review fix.
        sa_email = str(src.get("service_account_email", ""))
        instance_profile = ""
        if sa_email and "${" not in sa_email and "TODO-" not in sa_email:
            # extract local part of email as profile-name hint
            local = sa_email.split("@")[0]
            instance_profile = f"{local}-instance-profile"

        instances.append({
            "name":             name,
            "instance_type":    aws_type,
            "ami_hint":         ami_hint,
            "root_disk_size":   boot_disk_size,
            "root_disk_type":   aws_disk_type,
            "instance_profile": instance_profile,
            "network_tags":     gcp_network_tags,
            "_source_type":     gcp_type,
            "_source_image":    gcp_image,
            "_source_sa_email": sa_email,
        })

    # Translate top-level labels → AWS tags
    labels = args.get("labels") or {}
    if not isinstance(labels, dict):
        labels = {}

    if not instances:
        notes.append("No vm_configs / vms / instances found in source; emitted empty list.")
    else:
        notes.append(f"Emitted {len(instances)} EC2 instance entr{'y' if len(instances)==1 else 'ies'}.")
        notes.append("Service account → IAM instance profile (different attachment model).")
        notes.append("Boot disk image → AMI: operator must add `data \"aws_ami\"` data source per image; "
                     "see commented hints in inputs.")
        notes.append("GCP network tags (firewall targeting) → AWS Security Group association — not 1:1; "
                     "operator must define SGs that match the firewall rules from those tag-targeted rules.")
        notes.append("Metadata startup-script → user_data; OS Login → SSM Session Manager.")

    aws_inputs_hcl = (
        "  # Translated from GCP google_compute_instance vm_configs.\n"
        "  # Per-instance: machine type, AMI, root disk, IAM profile mapped from\n"
        "  # service account email. Review TODOs for VPC/subnet/SG wiring.\n"
        f"  vm_configs = {_render_vm_configs(instances)}\n"
        "\n"
        f"  tags = {_render_simple_map(labels)}\n"
        "\n"
        "  # TODO: wire to networking module outputs\n"
        '  vpc_id     = "vpc-TODO"\n'
        "  subnet_ids = []\n"
    )

    return Translation(
        service_name=SERVICE_NAME,
        aws_inputs_hcl=aws_inputs_hcl,
        notes=notes,
    )


def _render_vm_configs(instances: list) -> str:
    if not instances:
        return "[]"
    lines = ["["]
    for inst in instances:
        lines.append("    {")
        lines.append(f'      name              = "{inst["name"]}"')
        lines.append(f'      instance_type     = "{inst["instance_type"]}"     # GCP {inst["_source_type"]}')
        lines.append(f'      # ami_id          = data.aws_ami.<lookup>.id   # {inst["ami_hint"]}')
        lines.append(f'      root_disk_size_gb = {inst["root_disk_size"]}')
        lines.append(f'      root_disk_type    = "{inst["root_disk_type"]}"')
        if inst["instance_profile"]:
            lines.append(f'      instance_profile_name = "{inst["instance_profile"]}"  # from SA {inst["_source_sa_email"]}')
        else:
            lines.append('      # instance_profile_name = ""')
        if inst["network_tags"]:
            tag_list = ", ".join(f'"{t}"' for t in inst["network_tags"])
            lines.append(f'      # network_tags      = [{tag_list}]   # GCP firewall-targeting tags — operator wires SGs')
        lines.append("    },")
    lines.append("  ]")
    return "\n".join(lines)


def _render_simple_map(d: Dict[str, Any]) -> str:
    if not d:
        return "{}"
    lines = ["{"]
    for k, v in d.items():
        # Only render plain string values; skip complex (operator review).
        if isinstance(v, str):
            v_clean = v.replace('"', '\\"')
            lines.append(f'    "{k}" = "{v_clean}"')
        elif isinstance(v, (int, float, bool)):
            lines.append(f'    "{k}" = "{v}"')
        else:
            lines.append(f'    # "{k}" = ...  # source value too complex; review')
    lines.append("  }")
    return "\n".join(lines)


def aws_module_spec() -> AWSModuleSpec:
    return AWSModuleSpec(
        service_name=SERVICE_NAME,
        main_tf=_MAIN_TF,
        variables_tf=_VARIABLES_TF,
        outputs_tf=_OUTPUTS_TF,
        versions_tf=DEFAULT_VERSIONS_TF,
        readme_md=_README,
    )


_MAIN_TF = '''# AWS EC2 module — emitted by Cloud Lifecycle Intelligence Migrator.
# Translates GCP google_compute_instance vm_configs.

# Default AMI lookups — operator may override per VM via vm_configs[].ami_id.
data "aws_ami" "amazon_linux_2023" {
  most_recent = true
  owners      = ["amazon"]
  filter {
    name   = "name"
    values = ["al2023-ami-*-x86_64"]
  }
}

resource "aws_instance" "this" {
  for_each = { for vm in var.vm_configs : vm.name => vm }

  ami           = lookup(each.value, "ami_id", null) != null ? each.value.ami_id : data.aws_ami.amazon_linux_2023.id
  instance_type = each.value.instance_type
  subnet_id     = length(var.subnet_ids) > 0 ? var.subnet_ids[index(var.vm_configs[*].name, each.value.name) % length(var.subnet_ids)] : null

  vpc_security_group_ids = var.security_group_ids
  iam_instance_profile   = lookup(each.value, "instance_profile_name", null)

  root_block_device {
    volume_size = each.value.root_disk_size_gb
    volume_type = each.value.root_disk_type
    encrypted   = true
  }

  metadata_options {
    http_tokens                 = "required"   # IMDSv2 only — security baseline
    http_endpoint               = "enabled"
    http_put_response_hop_limit = 2
  }

  user_data = lookup(each.value, "user_data", null)

  tags = merge(
    var.tags,
    {
      Name = each.value.name
    },
  )
}
'''


_VARIABLES_TF = '''variable "vm_configs" {
  type = list(object({
    name                  = string
    instance_type         = string  # e.g. t3.medium, c5.xlarge
    root_disk_size_gb     = number
    root_disk_type        = string  # gp3, io2, etc.
    # Optional fields below — use lookup() in main.tf:
    # ami_id              = string  # override AMI per VM
    # instance_profile_name = string
    # network_tags        = list(string)
    # user_data           = string
  }))
  description = "List of EC2 VMs. Each becomes one aws_instance."
  default     = []
}

variable "vpc_id" {
  type        = string
  description = "VPC ID."
}

variable "subnet_ids" {
  type        = list(string)
  description = "Subnet IDs. Round-robin assigned to vm_configs."
  default     = []
}

variable "security_group_ids" {
  type        = list(string)
  description = "Security group IDs to attach to every instance."
  default     = []
}

variable "tags" {
  type    = map(string)
  default = {}
}
'''


_OUTPUTS_TF = '''output "instance_ids" {
  value = { for k, i in aws_instance.this : k => i.id }
  description = "Map of VM name -> EC2 instance ID."
}

output "instance_private_ips" {
  value = { for k, i in aws_instance.this : k => i.private_ip }
}

output "instance_public_ips" {
  value = { for k, i in aws_instance.this : k => i.public_ip }
}
'''


_README = '''# AWS EC2 Instance module

Translates GCP `google_compute_instance` resources.

## GCP→AWS mapping

| GCP | AWS | Notes |
|---|---|---|
| `instance_type: e2-highcpu-8` | `instance_type: c5.2xlarge` | Family + size match by intent |
| `boot_disk_image: windows-cloud/windows-2025` | data lookup → AMI | Use `data "aws_ami"` per image |
| `boot_disk_type: pd-standard` | `gp3` | Default AWS general-purpose SSD |
| `service_account_email` | `iam_instance_profile` | Different attachment model — wire profile separately |
| `metadata.startup-script` | `user_data` | Inline script content |
| `metadata.enable-oslogin` | (none) | Use SSM Session Manager instead |
| `tags = ["foo"]` (network tags) | Security Group attachment | Not 1:1 — define SGs that match the targeted firewall rules |

## Required wiring

Operator supplies:
- `vpc_id`, `subnet_ids` from networking module
- `security_group_ids` from a firewall module
- `instance_profile_name` per VM (link to the IAM module's translated SA)
- `ami_id` per VM if you want a specific AMI (otherwise Amazon Linux 2023 is default)
'''
