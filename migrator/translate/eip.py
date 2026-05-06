"""GCP google_compute_address / google_compute_global_address → AWS aws_eip.

Source pattern (from customer's net-address terragrunt.hcl):

    inputs = {
      project_id       = local._project.locals.project_id
      labels           = { stack = "pelican-dev" }
      global_addresses = {
        "pelican-lb-dev" = {}
      }
    }

We translate `global_addresses` (or `internal_addresses`) into a single
flat list of EIP entries our AWS module consumes.
"""

from __future__ import annotations

from typing import List, Optional

from migrator.results import DiscoveredResource

from .base import DEFAULT_VERSIONS_TF, AWSModuleSpec, Translation


SERVICE_NAME = "eip"


def translate(resource: DiscoveredResource) -> Translation:
    args = resource.arguments or {}
    notes: List[str] = []

    # GCP `global_addresses` map → AWS EIP list.
    global_addresses = args.get("global_addresses") or {}
    internal_addresses = args.get("internal_addresses") or {}

    eip_specs = []
    if isinstance(global_addresses, dict):
        for name, _cfg in global_addresses.items():
            eip_specs.append({
                "name":    str(name),
                "scope":   "global",
                "vpc":     True,
            })
    if isinstance(internal_addresses, dict):
        for name, cfg in internal_addresses.items():
            eip_specs.append({
                "name":    str(name),
                "scope":   "regional",
                "vpc":     True,
                "subnet":  (cfg or {}).get("subnetwork", "TODO-subnet-id")
                           if isinstance(cfg, dict) else "TODO-subnet-id",
            })

    if not eip_specs:
        notes.append("No global_addresses or internal_addresses found in source; "
                     "emitted empty eips list — review source inputs.")

    labels = args.get("labels") or {}
    if not isinstance(labels, dict):
        labels = {}

    # Render the inputs HCL block. Use minimal formatting so the
    # output is review-friendly.
    eips_hcl = _render_eip_list(eip_specs)
    tags_hcl = _render_tags(labels)

    aws_inputs_hcl = (
        "  # Translated from GCP global_addresses / internal_addresses.\n"
        "  # GCP static IPs become AWS Elastic IPs (one per entry).\n"
        f"  eips = {eips_hcl}\n"
        "\n"
        f"  tags = {tags_hcl}\n"
    )

    notes.append(
        f"Emitted {len(eip_specs)} EIP entr{'y' if len(eip_specs)==1 else 'ies'} "
        "(one per GCP address)."
    )
    return Translation(
        service_name=SERVICE_NAME,
        aws_inputs_hcl=aws_inputs_hcl,
        notes=notes,
    )


def _render_eip_list(specs: list) -> str:
    if not specs:
        return "{}"
    lines = ["{"]
    for s in specs:
        key = s["name"].replace("-", "_").replace(".", "_")
        lines.append(f'    "{key}" = {{')
        lines.append(f'      name  = "{s["name"]}"')
        lines.append(f'      scope = "{s["scope"]}"')
        if s.get("subnet") and s["scope"] == "regional":
            lines.append(f'      # subnet  = "{s["subnet"]}"  # TODO: wire to actual subnet ID')
        lines.append("    }")
    lines.append("  }")
    return "\n".join(lines)


def _render_tags(labels: dict) -> str:
    if not labels:
        return "{}"
    lines = ["{"]
    for k, v in labels.items():
        if not isinstance(v, str):
            v = str(v)
        lines.append(f'    "{k}" = "{v}"')
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


_MAIN_TF = '''# AWS Elastic IP module — emitted by Cloud Lifecycle Intelligence Migrator.
#
# Replaces the GCP google_compute_address / google_compute_global_address
# resource family. Each entry in var.eips becomes one aws_eip resource
# in the AWS VPC.
#
# To swap this module for a customer-supplied AWS module: replace this
# main.tf only. Keep variables.tf + outputs.tf so the input/output
# contract used by the terragrunt.hcl callers stays stable.

resource "aws_eip" "this" {
  for_each = var.eips

  domain = "vpc"

  tags = merge(
    var.tags,
    {
      Name  = each.value.name
      scope = each.value.scope
    },
  )
}
'''


_VARIABLES_TF = '''variable "eips" {
  type = map(object({
    name  = string
    scope = string  # "global" or "regional"
  }))
  description = "Map of EIP keys -> spec. Each entry creates one aws_eip resource."
  default     = {}
}

variable "tags" {
  type        = map(string)
  description = "Tags merged onto every EIP."
  default     = {}
}
'''


_OUTPUTS_TF = '''output "eip_addresses" {
  value = {
    for k, e in aws_eip.this : k => {
      id        = e.id
      public_ip = e.public_ip
      allocation_id = e.allocation_id
    }
  }
  description = "Map of EIP key -> {id, public_ip, allocation_id}."
}
'''


_README = '''# AWS Elastic IP module

Emitted by Cloud Lifecycle Intelligence Migrator. Translates GCP
`google_compute_address` / `google_compute_global_address` to AWS Elastic IPs.

## Input contract

```hcl
eips = {
  "ip-key-1" = { name = "my-app-eip", scope = "global" }
  "ip-key-2" = { name = "my-db-eip",  scope = "regional" }
}

tags = { project = "x", env = "dev" }
```

## Outputs

```hcl
eip_addresses = {
  "ip-key-1" = { id = "eipalloc-...", public_ip = "1.2.3.4", allocation_id = "..." }
}
```

## Swap path

To replace this module body with a customer-supplied AWS Elastic IP
module: edit only `main.tf`. The `variables.tf` + `outputs.tf` files
define the contract that callers (the leaf `terragrunt.hcl` files)
depend on — keep those stable.
'''
