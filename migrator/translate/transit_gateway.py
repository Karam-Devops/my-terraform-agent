"""GCP NCC (Network Connectivity Center) → AWS Transit Gateway.

Two source types map here:

  * google_network_connectivity_hub      → aws_ec2_transit_gateway
  * google_network_connectivity_spoke    → aws_ec2_transit_gateway_vpc_attachment

GCP NCC's hub-spoke topology translates almost directly to AWS TGW.
The hub becomes the TGW; each spoke VPC becomes one TGW VPC
attachment. Default routing in this module is "auto-associate +
auto-propagate" which gives full-mesh connectivity — operator can
override per attachment for STAR / non-mesh topologies.

Cross-account considerations: when the source's spokes live in
different AWS accounts (multi-account org), the TGW must be shared
via aws_ram_resource_share. The module's README spells out the
patterns; this translator emits the single-account form as the
default and notes the multi-account alternative.
"""

from __future__ import annotations

import re as _re
from typing import List

from migrator.results import DiscoveredResource

from .base import DEFAULT_VERSIONS_TF, AWSModuleSpec, Translation


SERVICE_NAME = "ec2-transit-gateway"


def translate(
    resource: DiscoveredResource,
    *,
    compliance_profile: str = "none",
) -> Translation:
    """Translate NCC hub (or spoke) into TGW resources.

    Source-shape detection: NCC hub modules typically carry the spokes
    list in their arguments (hub-driven), so we extract everything from
    one resource. When only a SPOKE resource is encountered (the hub
    is in a different stack), we emit a single-attachment scaffold
    pointing at the operator-supplied TGW ID.
    """
    args = resource.arguments or {}
    notes: List[str] = []
    tf_type = resource.tf_type

    # ---- Hub extraction (works for both hub and spoke-only paths) ----
    # Three source shapes:
    #   (a) top-level: hub_name = "..."
    #   (b) top-level: name = "..." (vanilla GCP module)
    #   (c) nested dict: hub = { name = "...", description = "..." }
    #       (DH customer pattern — see common-network/ncc source)
    hub_block = args.get("hub")
    if isinstance(hub_block, dict):
        hub_name = str(
            hub_block.get("name")
            or args.get("hub_name")
            or args.get("name")
            or "TODO-hub-name"
        )
        hub_description = str(
            hub_block.get("description")
            or args.get("description")
            or f"Migrated from GCP NCC: {hub_name}"
        )
    else:
        hub_name = str(
            args.get("hub_name")
            or args.get("name")
            or args.get("ncc_hub_name")
            or "TODO-hub-name"
        )
        hub_description = str(
            args.get("description")
            or f"Migrated from GCP NCC: {hub_name}"
        )

    # ---- Spoke extraction (DH and vanilla module shapes) ----
    raw_spokes = (
        args.get("spokes")
        or args.get("ncc_spokes")
        or args.get("spoke_configs")
        or args.get("vpc_spokes")
        or []
    )
    if isinstance(raw_spokes, dict):
        # dict-of-dicts → list-of-dicts (use the key as the spoke name)
        raw_spokes = [
            {**v, "name": v.get("name") or k}
            for k, v in raw_spokes.items()
            if isinstance(v, dict)
        ]
    if not isinstance(raw_spokes, list):
        raw_spokes = []

    spokes = []
    for s in raw_spokes:
        if not isinstance(s, dict):
            continue
        sname = str(s.get("name") or s.get("spoke_name") or "TODO-spoke")
        # GCP source uses `linked_vpc_network` (a self-link to a VPC).
        # DH variant uses bare `vpc_network`. AWS attachment needs a
        # vpc_id — operator wires this to the corresponding aws_vpc.
        linked = str(
            s.get("linked_vpc_network")
            or s.get("vpc_network")
            or s.get("consumer_vpc_uri")
            or s.get("vpc")
            or s.get("vpc_self_link")
            or "TODO-vpc-id"
        )
        # GCP subnets are regional; AWS attachments need explicit
        # subnet IDs (one per AZ). Translator can't infer the subnet
        # list — operator wires from the consumer-account VPC module.
        spokes.append({
            "name":   sname,
            "vpc":    linked,
        })

    # ---- Notes ----
    if not spokes and tf_type == "google_network_connectivity_hub":
        notes.append(
            "NCC hub had no spokes attribute in source — emitted hub-only "
            "TGW. Operator wires attachments separately."
        )
    elif spokes:
        notes.append(
            f"Emitted TGW + {len(spokes)} VPC attachment(s). "
            f"Each spoke's `linked_vpc_network` was a GCP project/network "
            "self-link; operator must replace with the AWS aws_vpc.id of "
            "the corresponding migrated VPC."
        )

    if tf_type == "google_network_connectivity_spoke":
        notes.append(
            "Standalone spoke resource (hub lives in another stack). "
            "Set var.transit_gateway_id from a remote-state lookup of the "
            "network/hub stack's output. Module emits ONE attachment block "
            "wired to var.transit_gateway_id + each.value.vpc_id."
        )

    notes.append(
        "Multi-account topology: TGW lives in a 'network' AWS account. "
        "Service-account VPCs attach via aws_ram_resource_share + "
        "aws_ram_principal_association. See modules/ec2-transit-gateway/README.md."
    )
    notes.append(
        "Default routing config = full-mesh (auto-associate + auto-propagate). "
        "STAR / hub-only topologies need explicit aws_ec2_transit_gateway_route "
        "entries — operator decides post-deploy."
    )

    aws_inputs_hcl = (
        "  # Translated from GCP NCC → AWS Transit Gateway.\n"
        f'  hub_name        = "{hub_name}"\n'
        f'  hub_description = "{hub_description}"\n'
        f"  spokes          = {_render_spokes(spokes)}\n"
    )

    return Translation(
        service_name=SERVICE_NAME,
        aws_inputs_hcl=aws_inputs_hcl,
        notes=notes,
    )


def _render_spokes(spokes: list) -> str:
    """Render the spokes input map. Empty when no spokes detected."""
    if not spokes:
        return "{}"
    lines = ["{"]
    for s in spokes:
        # Sanitize the spoke name into an HCL identifier-safe map key.
        raw_key = str(s["name"])
        clean = _re.sub(r"\$\{[^}]*\}", "", raw_key)
        clean = _re.sub(r"[^A-Za-z0-9_]+", "_", clean).strip("_")
        if not clean:
            clean = f"spoke_{len(lines)}"
        if clean[0].isdigit():
            clean = "_" + clean

        # Source `name` strings often contain function-wrapped
        # interpolation like `${dependency.X.outputs.Y["..."].name}-spoke`.
        # After the downstream sanitizer rewrites the inner reference,
        # the surrounding HCL is malformed (trailing `].name}-spoke"`
        # breaks the rendered string). Clean here so the emitted
        # `name = "..."` literal stays valid HCL.
        name_value = _clean_string_for_emission(str(s["name"]))
        # Same cleanup for the vpc-self-link comment — the comment is
        # truncated downstream but better to strip ${...} pieces here
        # so we don't slice mid-interpolation.
        vpc_hint = _clean_string_for_emission(str(s["vpc"]))[:60]

        lines.append(f'    "{clean}" = {{')
        lines.append(f'      name       = "{name_value}"')
        # vpc_id and subnet_ids are operator-supplied — emit cleanly-
        # named TODO defaults that the wiring layer will pick up.
        lines.append(f'      vpc_id     = "TODO-vpc-id"     # source linked_vpc_network: {vpc_hint}')
        lines.append('      subnet_ids = []                  # private subnets in the spoke VPC')
        lines.append("    }")
    lines.append("  }")
    return "\n".join(lines)


def _clean_string_for_emission(s: str) -> str:
    """Replace each balanced ``${...}`` chunk in ``s`` with a named TODO.

    Customer source strings include NESTED interpolations like
    ``${dependency.X.outputs.Y["${local.Z}-suffix"].name}-tail``.
    A naive regex matches `${...}` non-greedily and treats the inner
    `}` as the chunk boundary, leaving dangling `"].name}-tail"` text.

    This function uses brace-counting to find the OUTER balanced
    `${...}` boundary, then replaces the whole chunk with a TODO
    marker derived from the first known reference (local / var / each
    / dependency) inside.
    """
    if "${" not in s:
        return s

    out_chars: List[str] = []
    i = 0
    n = len(s)
    while i < n:
        # Look for the next `${`
        start = s.find("${", i)
        if start < 0:
            out_chars.append(s[i:])
            break
        # Copy text before the interpolation as-is
        out_chars.append(s[i:start])

        # Find the matching `}` with brace counting (handles nested `${`)
        depth = 1
        j = start + 2  # skip the opening `${`
        while j < n and depth > 0:
            if s[j] == "{" and j > 0 and s[j - 1] == "$":
                depth += 1
            elif s[j] == "}":
                depth -= 1
            j += 1

        # j now points past the matching `}` (or end-of-string if
        # unbalanced). Extract the inner expression.
        if depth != 0:
            # Unbalanced — strip the whole tail. Pathological input.
            out_chars.append("TODO-unresolved")
            break

        inner = s[start + 2:j - 1]
        out_chars.append(_named_todo_from_expr(inner))
        i = j

    return "".join(out_chars)


def _named_todo_from_expr(expr: str) -> str:
    """Synthesize a `TODO-<kind>-<slug>` marker from the first known
    reference (local / var / each / dependency) embedded in ``expr``."""
    for kind in ("local", "var", "each", "dependency"):
        mref = _re.search(rf"\b{kind}\.([A-Za-z0-9_.\-]+)", expr)
        if mref:
            slug = _re.sub(r"[^A-Za-z0-9_]+", "-", mref.group(1)).strip("-")
            return f"TODO-{kind}-{slug}"
    return "TODO-unresolved"


def aws_module_spec() -> AWSModuleSpec:
    return AWSModuleSpec(
        service_name=SERVICE_NAME,
        main_tf=_MAIN_TF,
        variables_tf=_VARIABLES_TF,
        outputs_tf=_OUTPUTS_TF,
        versions_tf=DEFAULT_VERSIONS_TF,
        readme_md=_README,
    )


_MAIN_TF = '''# AWS Transit Gateway module — emitted by Cloud Lifecycle Intelligence Migrator.
# Translates GCP NCC (Network Connectivity Center) hub-spoke topology.

resource "aws_ec2_transit_gateway" "this" {
  description                     = var.hub_description
  auto_accept_shared_attachments  = "enable"   # auto-accept cross-account RAM-shared attachments
  default_route_table_association = "enable"
  default_route_table_propagation = "enable"
  dns_support                     = "enable"
  vpn_ecmp_support                = "enable"

  tags = merge(var.tags, { Name = var.hub_name })
}

# One VPC attachment per spoke. Each spoke VPC must exist in the same
# AWS account as the TGW (single-account topology), OR be shared via
# RAM from another account (multi-account topology — see README).
resource "aws_ec2_transit_gateway_vpc_attachment" "spokes" {
  for_each = var.spokes

  transit_gateway_id = aws_ec2_transit_gateway.this.id
  vpc_id             = each.value.vpc_id
  subnet_ids         = each.value.subnet_ids

  transit_gateway_default_route_table_association = true
  transit_gateway_default_route_table_propagation = true

  tags = merge(var.tags, { Name = each.value.name })
}

# Optional: share the TGW with other AWS accounts in the org. Comment
# out the share + association blocks for single-account topologies.
resource "aws_ram_resource_share" "tgw" {
  count = var.enable_cross_account_share ? 1 : 0

  name                      = "${var.hub_name}-tgw-share"
  allow_external_principals = false

  tags = var.tags
}

resource "aws_ram_resource_association" "tgw" {
  count = var.enable_cross_account_share ? 1 : 0

  resource_arn       = aws_ec2_transit_gateway.this.arn
  resource_share_arn = aws_ram_resource_share.tgw[0].arn
}
'''


_VARIABLES_TF = '''variable "hub_name" {
  type        = string
  description = "TGW hub name (was GCP NCC hub name)."
}

variable "hub_description" {
  type        = string
  description = "TGW description."
  default     = "Migrated from GCP NCC"
}

variable "spokes" {
  type = map(object({
    name       = string
    vpc_id     = string
    subnet_ids = list(string)
  }))
  description = "Map of spoke key -> spec. Each becomes one aws_ec2_transit_gateway_vpc_attachment."
  default     = {}
}

variable "enable_cross_account_share" {
  type        = bool
  description = "When true, create aws_ram_resource_share + association so other AWS accounts in the org can attach their VPCs to this TGW. Each service account also needs an aws_ram_principal_association (configure separately)."
  default     = false
}

variable "tags" {
  type    = map(string)
  default = {}
}
'''


_OUTPUTS_TF = '''output "transit_gateway_id" {
  value       = aws_ec2_transit_gateway.this.id
  description = "TGW ID. Cross-account attachments reference this via the RAM share."
}

output "transit_gateway_arn" {
  value       = aws_ec2_transit_gateway.this.arn
  description = "TGW ARN — supply to aws_ram_resource_association when sharing across accounts."
}

output "attachment_ids" {
  value       = { for k, a in aws_ec2_transit_gateway_vpc_attachment.spokes : k => a.id }
  description = "Map of spoke key -> attachment ID. Use as input to per-attachment routes / route tables."
}

output "share_arn" {
  value       = var.enable_cross_account_share ? aws_ram_resource_share.tgw[0].arn : null
  description = "RAM share ARN (null when cross_account_share disabled)."
}
'''


_README = '''# AWS Transit Gateway module

Translates GCP NCC (Network Connectivity Center) hub-spoke topology to AWS TGW.

## Single-account vs multi-account

* **Single-account** (default): TGW + all spoke VPCs in one AWS account.
  Use this when the GCP source had a single project owning the hub +
  all spokes' VPCs.
* **Multi-account**: TGW lives in a "network" account; spoke VPCs live
  in service accounts and attach via TGW sharing. Set
  `enable_cross_account_share = true`. In each service account:

  ```hcl
  resource "aws_ram_principal_association" "tgw" {
    principal          = data.aws_caller_identity.current.account_id
    resource_share_arn = "<network-account-share-arn>"
  }
  resource "aws_ec2_transit_gateway_vpc_attachment" "this" {
    transit_gateway_id = "<tgw-id-from-network-account>"
    vpc_id             = aws_vpc.spoke.id
    subnet_ids         = aws_subnet.spoke_private[*].id
  }
  ```

## Routing

Default settings auto-associate + auto-propagate every attachment with
the default TGW route table — fine for full-mesh connectivity. For
STAR / non-mesh topologies, set
`transit_gateway_default_route_table_propagation = false` on the
attachments + write explicit `aws_ec2_transit_gateway_route` entries.

## Cost considerations

TGW pricing: per-attachment per-hour + per-GB data processed. For
small topologies (< 5 VPCs in one account), VPC peering can be
cheaper. See AWS docs for thresholds.
'''
