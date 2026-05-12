"""GCP Shared VPC → AWS multi-account network architecture.

Three TF source types map here:

  * google_compute_shared_vpc_host_project              → emit TGW hub
                                                           + RAM share
  * google_compute_shared_vpc_service_project_attachment → emit one
                                                           per-account
                                                           attachment hint

The paradigm shift is real: GCP Shared VPC is "one VPC, many projects
consume it via IAM" while AWS multi-account = "every account has its
OWN VPC, accounts talk via TGW or RAM-shared subnets".

This translator emits Strategy A (TGW hub-spoke) as the default —
the closest analog to the GCP model. The module's README documents
Strategy B (RAM subnet share) and Strategy C (single-account
collapse) for operators whose topology is simpler.
"""

from __future__ import annotations

import re as _re
from typing import List

from migrator.results import DiscoveredResource

from .base import DEFAULT_VERSIONS_TF, AWSModuleSpec, Translation


# Re-use the same TGW service module that NCC emits. Both source types
# converge on the same AWS resources; only the inputs differ.
SERVICE_NAME = "ec2-transit-gateway"


def translate(
    resource: DiscoveredResource,
    *,
    compliance_profile: str = "none",
) -> Translation:
    """Translate Shared VPC host_project → TGW + cross-account share.

    The host project becomes the "network" AWS account housing the TGW.
    Service projects become separate AWS accounts that attach via
    aws_ram_resource_share. We emit:
      * The TGW (single-account form; operator turns on cross-account
        share via the variable)
      * Comment block listing the service projects so the operator can
        wire matching attachments in each service account
    """
    args = resource.arguments or {}
    notes: List[str] = []

    # Source patterns observed:
    #   * google_compute_shared_vpc_host_project: top-level `project`
    #   * google_compute_shared_vpc_service_project_attachment: top-
    #     level `host_project` + `service_project`
    host_project = str(
        args.get("project")
        or args.get("host_project")
        or args.get("host_project_id")
        or "TODO-host-project"
    )

    # Service projects: collected either from this single resource or
    # from a list of attachments inline.
    raw_services = (
        args.get("service_projects")
        or args.get("attached_projects")
        or args.get("service_project_ids")
        or []
    )
    if isinstance(raw_services, str):
        raw_services = [raw_services]
    if not isinstance(raw_services, list):
        raw_services = []
    # If we got a single attachment resource, pick its `service_project` field.
    single_service = args.get("service_project")
    if single_service and not raw_services:
        raw_services = [single_service]

    service_projects = [str(s) for s in raw_services if s]

    if not service_projects:
        notes.append(
            "No service projects detected in this resource — emitted TGW "
            "hub-only. Add aws_ec2_transit_gateway_vpc_attachment entries "
            "in service-account roots after the host TGW is shared."
        )
    else:
        notes.append(
            f"Source declared {len(service_projects)} service project(s). "
            "Each becomes its own AWS account with a VPC attached to this "
            "TGW via aws_ram_resource_share. Operator must:"
        )
        notes.append(
            "  1. Create one AWS account per service project (via "
            "Control Tower or aws_organizations_account)."
        )
        notes.append(
            "  2. Provision a VPC + subnets in each service account."
        )
        notes.append(
            "  3. Add aws_ram_principal_association in this network-account "
            "root that points at each service-account ID."
        )
        notes.append(
            "  4. In each service account, add "
            "aws_ec2_transit_gateway_vpc_attachment referencing the "
            "shared TGW ID."
        )

    notes.append(
        "Paradigm shift: GCP Shared VPC = 1 VPC, many projects consume "
        "it via IAM. AWS multi-account = N VPCs (one per account), "
        "accounts talk via TGW. NOT a 1:1 translation."
    )
    notes.append(
        "Alternative strategies:"
    )
    notes.append(
        "  Strategy B (simpler): aws_ram_resource_share on SUBNETS so "
        "all accounts share one VPC's subnets directly. Loses isolation "
        "but cheaper to operate. See module README."
    )
    notes.append(
        "  Strategy C (smallest blast radius): collapse host + service "
        "projects into ONE AWS account with one VPC. Loses the project "
        "boundary entirely but eliminates all cross-account complexity."
    )

    # Emit a TGW configured with cross-account sharing turned ON by
    # default (operator opts out if they're doing single-account).
    aws_inputs_hcl = (
        "  # Translated from GCP Shared VPC → AWS TGW hub-spoke (Strategy A).\n"
        f'  hub_name        = "{_sanitize_id(host_project)}-tgw"\n'
        f'  hub_description = "Shared VPC host project {host_project} → TGW network account hub"\n'
        "  spokes          = {}   # service-project VPCs attach from THEIR accounts\n"
        "  enable_cross_account_share = true   # Shared VPC pattern requires this\n"
    )
    if service_projects:
        aws_inputs_hcl += (
            "\n"
            "  # Source declared these service projects — wire matching\n"
            "  # aws_ram_principal_association blocks (per service account):\n"
        )
        for sp in service_projects[:10]:
            aws_inputs_hcl += f"  #   - {sp}\n"
        if len(service_projects) > 10:
            aws_inputs_hcl += (
                f"  #   ... and {len(service_projects) - 10} more "
                "(see source for full list)\n"
            )

    return Translation(
        service_name=SERVICE_NAME,
        aws_inputs_hcl=aws_inputs_hcl,
        notes=notes,
    )


def _sanitize_id(s: str) -> str:
    """Convert a GCP project ID into a clean AWS-friendly identifier."""
    clean = _re.sub(r"\$\{[^}]*\}", "", str(s))
    clean = _re.sub(r"[^A-Za-z0-9_-]+", "-", clean).strip("-")
    return clean or "shared-vpc-host"


def aws_module_spec() -> AWSModuleSpec:
    """Shared VPC uses the SAME module as NCC (both converge on TGW).
    Returning the spec here would emit a duplicate module body —
    instead we declare the service_name so the dispatcher matches but
    let transit_gateway.aws_module_spec() be the single source of truth
    for the module body. The translate_resource dispatcher de-dupes by
    service_name."""
    # Import inside the function so module import order doesn't matter.
    from . import transit_gateway
    return transit_gateway.aws_module_spec()
