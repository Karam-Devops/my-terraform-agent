"""GCP HA VPN → AWS Site-to-Site VPN.

Source pattern (DH net-vpn-ha module):

    inputs = {
      vpn_configs = [
        {
          name    = "ha-vpn-X-to-aws"
          network = "projects/.../networks/vpc-X-shared"
          peer_gateways.default.external.interfaces = ["A.B.C.D", ...]
          router_config = { asn, custom_advertise.ip_ranges, ... }
          tunnels = {
            remote-0 = { bgp_peer = {address, asn}, shared_secret = ..., ...}
            remote-1 = {...}
          }
        }
      ]
    }

AWS shape:
  * aws_vpn_gateway       — one per vpn_configs entry, attached to VPC
  * aws_customer_gateway  — one per peer-external-interface IP
  * aws_vpn_connection    — one per tunnel (refs customer GW + VPN GW)
                            shared_secret → tunnel1_preshared_key
  * BGP ASN translates 1:1 (router_config.asn → customer_gateway.bgp_asn)
  * Static routes via custom_advertise.ip_ranges → aws_vpn_connection_route

Shared secrets typically came from environment vars in GCP source
(`get_env("GCP_AWS_NONPROD_SECRET_TUNNEL1")`). The translator can't
read those — emits `var.tunnel_X_preshared_key` and lets the operator
supply via tfvars or pull from AWS Secrets Manager.
"""

from __future__ import annotations

import re as _re
from typing import List

from migrator.results import DiscoveredResource

from .base import DEFAULT_VERSIONS_TF, AWSModuleSpec, Translation


SERVICE_NAME = "site-to-site-vpn"


def translate(
    resource: DiscoveredResource,
    *,
    compliance_profile: str = "none",
) -> Translation:
    """Translate GCP HA VPN config → AWS Site-to-Site VPN resources."""
    args = resource.arguments or {}
    notes: List[str] = []

    # Source-shape variants:
    #   * vpn_configs    — DH (list-of-dicts)
    #   * vpn_gateway    — vanilla GCP module top-level dict
    #   * tunnels        — when only tunnel info is present (no GW)
    raw_configs = (
        args.get("vpn_configs")
        or args.get("vpn_gateway_configs")
        or args.get("vpns")
        or []
    )
    if isinstance(raw_configs, dict):
        # Single top-level config wrapped in a list for uniform iteration.
        raw_configs = [raw_configs]
    if not isinstance(raw_configs, list):
        raw_configs = []

    # Fallback: synthesize a single-entry config from top-level scalars
    # when the source defined the VPN inline (not via vpn_configs).
    if not raw_configs and args.get("name"):
        raw_configs = [{
            "name":           args.get("name"),
            "network":        args.get("network", ""),
            "tunnels":        args.get("tunnels") or {},
            "router_config":  args.get("router_config") or {},
            "peer_gateways":  args.get("peer_gateways") or {},
        }]

    gateways = []
    for cfg in raw_configs:
        if not isinstance(cfg, dict):
            continue
        gw_name = str(cfg.get("name", "TODO-vpn-gateway"))
        # Extract peer external interface IPs (list of strings).
        peer_ips: List[str] = []
        peer_gw = cfg.get("peer_gateways") or {}
        if isinstance(peer_gw, dict):
            default_gw = peer_gw.get("default") or {}
            if isinstance(default_gw, dict):
                external = default_gw.get("external") or {}
                if isinstance(external, dict):
                    raw_ifs = external.get("interfaces") or []
                    if isinstance(raw_ifs, list):
                        peer_ips = [str(ip) for ip in raw_ifs]

        # BGP ASN from router_config (operator supplies AWS-side ASN via var)
        router_cfg = cfg.get("router_config") or {}
        bgp_asn = router_cfg.get("asn") if isinstance(router_cfg, dict) else None

        # Tunnels: dict-of-dicts. Each tunnel has bgp_peer + shared_secret.
        tunnels = []
        raw_tunnels = cfg.get("tunnels") or {}
        if isinstance(raw_tunnels, dict):
            for tkey, t in raw_tunnels.items():
                if not isinstance(t, dict):
                    continue
                bgp_peer = t.get("bgp_peer") or {}
                tunnels.append({
                    "key":              _safe_key(str(tkey)),
                    "source_name":      str(tkey),
                    "bgp_peer_address": str(bgp_peer.get("address", "")) if isinstance(bgp_peer, dict) else "",
                    "bgp_peer_asn":     bgp_peer.get("asn") if isinstance(bgp_peer, dict) else None,
                    # Source uses shared_secret = get_env(...) which can't
                    # be statically resolved; surface as a per-tunnel var.
                    "_secret_source":   str(t.get("shared_secret", ""))[:80],
                })

        gateways.append({
            "name":     _clean_for_emission(gw_name),
            "peer_ips": peer_ips,
            "bgp_asn":  bgp_asn,
            "tunnels":  tunnels,
        })

    # ---- Notes ----
    if not gateways:
        notes.append(
            "No VPN config detected in source — emitted single placeholder "
            "VPN gateway. Operator wires customer/VPN GW IDs manually."
        )
    else:
        n_tunnels = sum(len(g["tunnels"]) for g in gateways)
        notes.append(
            f"Emitted {len(gateways)} VPN gateway(s) + "
            f"{n_tunnels} tunnel connection(s)."
        )
    notes.append(
        "Shared secrets came from get_env(...) in GCP source — non-static. "
        "Module declares one `var.tunnel_X_preshared_key` per tunnel; "
        "supply via tfvars or AWS Secrets Manager data lookup."
    )
    notes.append(
        "BGP ASN: source router_config.asn → aws_customer_gateway.bgp_asn. "
        "AWS-side ASN (aws_vpn_gateway.amazon_side_asn) defaults to 64512 — "
        "override if your AWS account uses a different ASN."
    )
    notes.append(
        "Static routes: source `router_config.custom_advertise.ip_ranges` "
        "should become `aws_vpn_connection_route` blocks. Translator emits "
        "the BGP-driven baseline; explicit routes are a separate operator pass."
    )

    aws_inputs_hcl = (
        "  # Translated from GCP HA VPN → AWS Site-to-Site VPN.\n"
        f"  gateways = {_render_gateways(gateways)}\n"
    )

    return Translation(
        service_name=SERVICE_NAME,
        aws_inputs_hcl=aws_inputs_hcl,
        notes=notes,
    )


def _safe_key(s: str) -> str:
    """HCL identifier-safe key."""
    clean = _re.sub(r"\$\{[^}]*\}", "", s)
    clean = _re.sub(r"[^A-Za-z0-9_]+", "_", clean).strip("_")
    if not clean:
        return "tunnel"
    if clean[0].isdigit():
        clean = "_" + clean
    return clean


def _clean_for_emission(s: str) -> str:
    """Strip `${...}` chunks from values emitted inline."""
    return _re.sub(r"\$\{[^}]*\}", "", s).strip()


def _render_gateways(gateways: list) -> str:
    if not gateways:
        return "{}"
    lines = ["{"]
    for g in gateways:
        gkey = _safe_key(g["name"])
        lines.append(f'    "{gkey}" = {{')
        lines.append(f'      name    = "{g["name"]}"')
        # Peer IPs become aws_customer_gateway resources inside the module.
        if g["peer_ips"]:
            ips = ", ".join(f'"{ip}"' for ip in g["peer_ips"])
            lines.append(f'      peer_ips = [{ips}]')
        else:
            lines.append('      peer_ips = []   # TODO: supply remote endpoint IPs')
        if g["bgp_asn"] is not None:
            lines.append(f'      bgp_asn = {g["bgp_asn"]}')
        else:
            lines.append('      bgp_asn = 65000   # TODO: source had no router_config.asn')
        # Tunnels
        if g["tunnels"]:
            lines.append("      tunnels = {")
            for t in g["tunnels"]:
                lines.append(f'        "{t["key"]}" = {{')
                lines.append(f'          # source tunnel name: {t["source_name"]}')
                if t["bgp_peer_address"]:
                    lines.append(f'          bgp_peer_address = "{t["bgp_peer_address"]}"')
                if t["bgp_peer_asn"] is not None:
                    lines.append(f'          bgp_peer_asn     = {t["bgp_peer_asn"]}')
                # Secret source was get_env() — name a per-tunnel var.
                var_name = f"vpn_preshared_key_{gkey}_{t['key']}"
                lines.append(f'          preshared_key    = var.{var_name}')
                lines.append("        }")
            lines.append("      }")
        else:
            lines.append('      tunnels = {}')
        lines.append("    }")
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


_MAIN_TF = '''# AWS Site-to-Site VPN module — emitted by Cloud Lifecycle Intelligence Migrator.
# Translates GCP HA VPN configs to AWS aws_vpn_gateway + customer_gateway + vpn_connection.

# Flatten the {gateway → tunnel} 2-level map into a flat key-set for
# tunnel resources. Each entry is `<gw_key>__<tunnel_key>`.
locals {
  flat_tunnels = merge([
    for gk, g in var.gateways : {
      for tk, t in g.tunnels : "${gk}__${tk}" => merge(t, { gw_key = gk, tunnel_key = tk })
    }
  ]...)
}

# One aws_vpn_gateway per source vpn_config entry, attached to the
# operator-supplied VPC. (Cross-module wiring fills in vpc_id.)
resource "aws_vpn_gateway" "this" {
  for_each = var.gateways

  vpc_id          = var.vpc_id
  amazon_side_asn = var.amazon_side_asn

  tags = merge(var.tags, { Name = each.value.name })
}

# Customer gateways — one per remote peer IP. The peer_ips list inside
# each gateway entry maps to N customer_gateways (DH typically uses 2
# for HA / TWO_IPS_REDUNDANCY).
locals {
  customer_gateways = merge([
    for gk, g in var.gateways : {
      for idx, ip in g.peer_ips : "${gk}__${idx}" => {
        gw_key = gk
        ip     = ip
        asn    = g.bgp_asn
      }
    }
  ]...)
}

resource "aws_customer_gateway" "this" {
  for_each = local.customer_gateways

  bgp_asn    = each.value.asn
  ip_address = each.value.ip
  type       = "ipsec.1"

  tags = merge(var.tags, { Name = "${each.value.gw_key}-${each.key}" })
}

# Tunnels: one aws_vpn_connection per tunnel entry. Each connection
# binds one customer_gateway + one vpn_gateway. preshared_key is a
# required per-tunnel input (var.X) supplied via tfvars.
resource "aws_vpn_connection" "this" {
  for_each = local.flat_tunnels

  vpn_gateway_id      = aws_vpn_gateway.this[each.value.gw_key].id
  # Match customer GW by index — first tunnel gets index 0's customer GW.
  customer_gateway_id = [
    for k, v in local.customer_gateways : aws_customer_gateway.this[k].id
    if v.gw_key == each.value.gw_key
  ][0]
  type = "ipsec.1"

  tunnel1_preshared_key = each.value.preshared_key

  static_routes_only = false

  tags = merge(var.tags, { Name = "${each.value.gw_key}-${each.value.tunnel_key}" })
}
'''


_VARIABLES_TF = '''variable "gateways" {
  type = map(object({
    name     = string
    peer_ips = list(string)
    bgp_asn  = number
    tunnels  = map(object({
      bgp_peer_address = optional(string, "")
      bgp_peer_asn     = optional(number, 64512)
      preshared_key    = string
    }))
  }))
  description = "Map of vpn gateway key -> spec. Each becomes one aws_vpn_gateway + N customer_gateways + N vpn_connections."
  default     = {}
}

variable "vpc_id" {
  type        = string
  description = "VPC ID to attach the VPN gateways to. Wired automatically by the cross-module layer when a VPC module is in the same env."
}

variable "amazon_side_asn" {
  type        = number
  description = "AWS-side BGP ASN for VPN gateways. AWS default is 64512; override for shops with their own ASN allocation."
  default     = 64512
}

variable "tags" {
  type    = map(string)
  default = {}
}
'''


_OUTPUTS_TF = '''output "vpn_gateway_ids" {
  value       = { for k, g in aws_vpn_gateway.this : k => g.id }
  description = "Map of gateway key -> VPN gateway ID."
}

output "vpn_connection_ids" {
  value       = { for k, c in aws_vpn_connection.this : k => c.id }
  description = "Map of tunnel key -> VPN connection ID."
}

output "tunnel_bgp_addresses" {
  value = {
    for k, c in aws_vpn_connection.this :
    k => {
      tunnel1_address      = c.tunnel1_address
      tunnel1_cgw_inside   = c.tunnel1_cgw_inside_address
      tunnel1_vgw_inside   = c.tunnel1_vgw_inside_address
      tunnel1_bgp_asn      = c.tunnel1_bgp_asn
    }
  }
  description = "Per-tunnel AWS-side BGP addresses. Configure the remote BGP peer with these."
}
'''


_README = '''# AWS Site-to-Site VPN module

Translates GCP HA VPN → AWS Site-to-Site VPN. Produces:
- One `aws_vpn_gateway` per source `vpn_configs[]` entry (attached to VPC)
- N `aws_customer_gateway` per peer IP (typically 2 for HA)
- N `aws_vpn_connection` per tunnel (with BGP + preshared key)

## Required tfvars

For each tunnel, supply the preshared key via a tfvar:

```hcl
# terraform.tfvars
vpn_preshared_key_<gw_key>_<tunnel_key> = "actual-shared-secret"
```

These were `get_env(...)` lookups in the GCP source — pull from your
secret store (Secrets Manager / Parameter Store) the same way.

## BGP vs static routing

Module defaults to BGP (`static_routes_only = false`). For dynamic
route exchange this is correct. For static-only operators, set
`static_routes_only = true` per connection and add
`aws_vpn_connection_route` blocks listing each remote CIDR.

## AWS-side ASN

`amazon_side_asn` defaults to 64512. If your AWS Organization
already uses a different ASN (e.g., assigned during onboarding),
override per environment.
'''
