"""GCP google_container_cluster (GKE) → AWS aws_eks_cluster.

Source patterns (Customer's terragrunt inputs, varies):

    # Pattern A: top-level gke_cluster_name + vpc_config + nodepool_config
    inputs = {
      gke_cluster_name = "dh-gke-os-std-dev-cluster"
      vpc_config = {
        network    = "projects/.../vpc-shared"
        subnetwork = "projects/.../subnetworks/sb-..."
        master_ipv4_cidr_block = "172.27.140.0/28"
      }
      nodepool_config = [{
        node_config     = { machine_type = "e2-standard-4", disk_size_gb = 100 }
        nodepool_config = { autoscaling = { min_node_count = 4, max_node_count = 13 } }
      }]
    }

    # Pattern B: gke_config map (multi-cluster per env)
    inputs = {
      gke_config = {
        "us-central1" = {
          "cluster-name-1" = { gke_cluster_name = "...", master_ipv4_cidr_block = "..." }
        }
      }
    }

GKE → EKS architectural shifts the operator needs to know:

  * Control plane: GKE manages it implicitly; EKS exposes the API server
    as a configurable endpoint. HIPAA defaults: endpoint_public_access=false.
  * Workload identity (GKE) → IRSA (IAM Roles for Service Accounts). EKS
    creates an OIDC provider; ServiceAccounts annotate role ARNs.
  * Node pools: GKE has type+image+autoscaling. EKS aws_eks_node_group
    is similar but distinguishes managed vs self-managed; we emit
    managed for simplicity.
  * Secrets encryption: GKE app-layer secrets encryption with Cloud KMS
    → EKS `encryption_config.resources = ["secrets"]` with a KMS CMK.
  * Logging: GKE Cloud Logging always-on → EKS opt-in
    cluster_log_types. HIPAA defaults enable api, audit, authenticator.
"""

from __future__ import annotations

import re
from typing import List

from migrator.results import DiscoveredResource

from .base import DEFAULT_VERSIONS_TF, AWSModuleSpec, Translation


SERVICE_NAME = "eks-cluster"


# GCP machine_type → AWS EC2 instance type (compute-optimized parity).
# Reference: Kiro analysis recommendations + AWS pricing-equivalent map.
# We default to Graviton (Arm) where parity exists — cheaper and more
# energy-efficient. Operator can override per-cluster post-emission.
_MACHINE_TYPE_MAP = {
    # e2 series (cost-optimized) → Graviton t/m
    "e2-micro":         "t4g.micro",
    "e2-small":         "t4g.small",
    "e2-medium":        "t4g.medium",
    "e2-standard-2":    "m7g.large",
    "e2-standard-4":    "m7g.xlarge",
    "e2-standard-8":    "m7g.2xlarge",
    "e2-standard-16":   "m7g.4xlarge",
    "e2-standard-32":   "m7g.8xlarge",
    # n2 series (general purpose) → m7i (Intel) for x86 workload parity
    "n2-standard-2":    "m7i.large",
    "n2-standard-4":    "m7i.xlarge",
    "n2-standard-8":    "m7i.2xlarge",
    "n2-standard-16":   "m7i.4xlarge",
    "n2-standard-32":   "m7i.8xlarge",
    # n2d series (AMD) → m7a
    "n2d-standard-2":   "m7a.large",
    "n2d-standard-4":   "m7a.xlarge",
    "n2d-standard-8":   "m7a.2xlarge",
    # c2 / c2d (compute-optimized) → c7g / c7a
    "c2-standard-4":    "c7g.xlarge",
    "c2-standard-8":    "c7g.2xlarge",
    "c2-standard-16":   "c7g.4xlarge",
    "c2d-standard-4":   "c7a.xlarge",
    "c2d-standard-8":   "c7a.2xlarge",
    # m1 (memory-optimized) → r7g (Graviton memory)
    "m1-megamem-96":    "r7g.16xlarge",
    "m1-ultramem-40":   "r7g.8xlarge",
}


def _map_machine_type(gcp_type: str) -> str:
    """Map a GCP machine_type to an AWS instance type. Falls back to
    m7g.xlarge for unknown shapes; operator overrides post-emission."""
    if not gcp_type:
        return "m7g.xlarge"
    mapped = _MACHINE_TYPE_MAP.get(gcp_type)
    if mapped:
        return mapped
    # Heuristic for unrecognized custom shapes like "custom-N-M":
    m = re.match(r"custom-(\d+)-(\d+)", gcp_type)
    if m:
        cpus = int(m.group(1))
        if cpus >= 16:  return "m7g.4xlarge"
        if cpus >= 8:   return "m7g.2xlarge"
        if cpus >= 4:   return "m7g.xlarge"
        if cpus >= 2:   return "m7g.large"
        return "m7g.medium"
    return "m7g.xlarge"


def translate(
    resource: DiscoveredResource,
    *,
    compliance_profile: str = "none",
) -> Translation:
    """Translate GKE cluster → EKS cluster.

    Compliance profile defaults:
      - endpoint_public_access: forced False under HIPAA/SOC2/PCI
      - encryption_secrets: forced True under HIPAA/PCI
      - logging_enabled_types: full set under HIPAA/PCI, minimal under SOC2
      - irsa_required: forced True under HIPAA (no static IAM credentials on nodes)
    """
    from migrator.translate.compliance_profiles import get_defaults
    _profile_defaults = get_defaults(compliance_profile, "eks")

    args = resource.arguments or {}
    notes: List[str] = []

    # ---- Extract clusters from various source shapes ----
    clusters = []

    # Pattern A: top-level gke_cluster_name + nodepool_config (single cluster)
    if "gke_cluster_name" in args:
        clusters.append({
            "name":           str(args.get("gke_cluster_name", "TODO-cluster-name")),
            "vpc_config":     args.get("vpc_config") or {},
            "nodepool_config": args.get("nodepool_config") or [],
            "_source_pattern": "gke_cluster_name",
        })

    # Pattern B: gke_config map (region → {cluster_name → spec})
    gke_config = args.get("gke_config")
    if isinstance(gke_config, dict):
        for region_key, region_spec in gke_config.items():
            if isinstance(region_spec, dict):
                for cluster_key, spec in region_spec.items():
                    if isinstance(spec, dict):
                        clusters.append({
                            "name":           str(spec.get("gke_cluster_name", cluster_key)),
                            "vpc_config":     spec.get("vpc_config") or {},
                            "nodepool_config": spec.get("nodepool_config") or args.get("nodepool_config") or [],
                            "_source_pattern": "gke_config",
                            "_master_cidr":   spec.get("master_ipv4_cidr_block", ""),
                        })

    if not clusters:
        # Fall back to a single placeholder so the operator sees the structure.
        clusters.append({
            "name":            args.get("name", resource.name),
            "vpc_config":      {},
            "nodepool_config": [],
            "_source_pattern": "fallback",
        })
        notes.append("Could not detect gke_cluster_name or gke_config; emitted placeholder.")

    # ---- Per-cluster translation ----
    eks_specs = []
    for src in clusters:
        cluster_name = src["name"]
        # Translate nodepools.
        node_groups = []
        for np in src.get("nodepool_config", []):
            if not isinstance(np, dict):
                continue
            node_cfg = np.get("node_config") or {}
            np_cfg = np.get("nodepool_config") or {}
            autoscaling = np_cfg.get("autoscaling") or {}

            gcp_machine = str(node_cfg.get("machine_type", "e2-standard-4"))
            instance_type = _map_machine_type(gcp_machine)
            disk_size = int(node_cfg.get("disk_size_gb", 100) or 100)
            min_count = int(autoscaling.get("min_node_count", 1) or 1)
            max_count = int(autoscaling.get("max_node_count", 3) or 3)

            np_name = str(np_cfg.get("name", np.get("name", "default")))
            node_groups.append({
                "name":              np_name,
                "instance_types":    [instance_type],
                "min_size":          min_count,
                "max_size":          max_count,
                "desired_size":      min_count,
                "disk_size_gb":      disk_size,
                "_source_machine":   gcp_machine,
            })

        if not node_groups:
            # Default node group when source had no nodepool_config
            node_groups.append({
                "name":            "default",
                "instance_types":  ["m7g.xlarge"],
                "min_size":        2,
                "max_size":        8,
                "desired_size":    2,
                "disk_size_gb":    100,
                "_source_machine": "(none)",
            })

        eks_specs.append({
            "name":          cluster_name,
            "node_groups":   node_groups,
            "_master_cidr":  src.get("_master_cidr", ""),
        })

    # ---- Compliance profile derived attrs ----
    endpoint_public_access = _profile_defaults.get("endpoint_public_access")
    if endpoint_public_access is None:
        endpoint_public_access = True   # default: public + private both on (neutral)
    encryption_secrets = bool(_profile_defaults.get("encryption_secrets", False))
    logging_types = _profile_defaults.get("logging_enabled_types") or []
    irsa_required = bool(_profile_defaults.get("irsa_required", False))

    # ---- Notes ----
    notes.insert(0, f"Emitted {len(eks_specs)} EKS cluster(s) with "
                    f"{sum(len(c['node_groups']) for c in eks_specs)} managed node group(s).")
    notes.append("GKE Workload Identity → AWS IRSA. EKS module provisions an OIDC provider for the cluster; "
                 "ServiceAccounts must annotate `eks.amazonaws.com/role-arn=...` to assume IAM roles.")
    notes.append("GKE control plane CIDR (master_ipv4_cidr_block) → EKS doesn't expose a control-plane CIDR; "
                 "API server is reached via the cluster endpoint URL. Operator can restrict via cluster_endpoint_private_access + public_access_cidrs.")
    if compliance_profile and compliance_profile != "none":
        hardened = []
        if endpoint_public_access is False: hardened.append("endpoint_public_access=false")
        if encryption_secrets: hardened.append("secrets-envelope-encryption (KMS)")
        if logging_types: hardened.append(f"control-plane logging={'+'.join(logging_types)}")
        if irsa_required: hardened.append("IRSA OIDC provider")
        if hardened:
            notes.append(
                f"compliance profile '{compliance_profile.upper()}' applied — "
                f"defaults forced on: {', '.join(hardened)}"
            )

    # ---- Render inputs HCL ----
    aws_inputs_hcl = (
        "  # Translated from GCP google_container_cluster (GKE) → EKS.\n"
        "  # See module body for IRSA + secrets-encryption resource wiring.\n"
        f"  clusters = {_render_clusters(eks_specs)}\n"
    )

    # Profile-driven top-level inputs.
    if endpoint_public_access is not None:
        aws_inputs_hcl += f"\n  endpoint_public_access = {str(endpoint_public_access).lower()}"
        if endpoint_public_access is False:
            aws_inputs_hcl += "   # compliance profile"
        aws_inputs_hcl += "\n"
    if encryption_secrets:
        aws_inputs_hcl += "  encryption_secrets     = true    # compliance profile (envelope-encrypt K8s Secrets via KMS)\n"
    if logging_types:
        types_str = ", ".join(f'"{t}"' for t in logging_types)
        aws_inputs_hcl += f"  logging_enabled_types  = [{types_str}]   # compliance profile\n"
    if irsa_required:
        aws_inputs_hcl += "  enable_irsa            = true    # compliance profile (OIDC provider for ServiceAccount → IAM role mapping)\n"

    aws_inputs_hcl += (
        "\n"
        "  # TODO: wire to networking module outputs\n"
        '  vpc_id     = "TODO-vpc-id"\n'
        "  subnet_ids = []   # private subnets for nodes\n"
    )

    return Translation(
        service_name=SERVICE_NAME,
        aws_inputs_hcl=aws_inputs_hcl,
        notes=notes,
    )


def _render_clusters(specs: list) -> str:
    if not specs:
        return "{}"
    lines = ["{"]
    for c in specs:
        key = c["name"].replace("-", "_").replace(".", "_")
        lines.append(f'    "{key}" = {{')
        lines.append(f'      name            = "{c["name"]}"')
        lines.append(f'      cluster_version = "1.31"   # operator picks K8s version')
        lines.append("      node_groups = {")
        for ng in c["node_groups"]:
            ng_key = ng["name"].replace("-", "_")
            lines.append(f'        "{ng_key}" = {{')
            lines.append(f'          name           = "{ng["name"]}"')
            lines.append(f'          instance_types = {ng["instance_types"]}'.replace("'", '"') +
                         f'    # GCP {ng["_source_machine"]}')
            lines.append(f'          min_size       = {ng["min_size"]}')
            lines.append(f'          max_size       = {ng["max_size"]}')
            lines.append(f'          desired_size   = {ng["desired_size"]}')
            lines.append(f'          disk_size_gb   = {ng["disk_size_gb"]}')
            lines.append("        }")
        lines.append("      }")
        lines.append("    }")
    lines.append("  }")
    return "\n".join(lines)


def aws_module_spec() -> AWSModuleSpec:
    return AWSModuleSpec(
        service_name=SERVICE_NAME,
        main_tf=_MAIN_TF,
        variables_tf=_VARIABLES_TF,
        outputs_tf=_OUTPUTS_TF,
        versions_tf=_EKS_VERSIONS_TF,
        readme_md=_README,
    )


# EKS module needs the `tls` provider for the OIDC certificate fingerprint
# data source (only used when IRSA is enabled). Adding it here so DEFAULT_VERSIONS_TF
# stays minimal for the other modules.
_EKS_VERSIONS_TF = """terraform {
  required_version = ">= 1.5.0"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.20"
    }
    tls = {
      source  = "hashicorp/tls"
      version = "~> 4.0"
    }
  }
}
"""


_MAIN_TF = '''# AWS EKS module — emitted by Cloud Lifecycle Intelligence Migrator.
# Translates GCP google_container_cluster (GKE) + node pools.
#
# Provisions per cluster:
#   - aws_eks_cluster (control plane)
#   - aws_eks_node_group (one per source nodepool, managed)
#   - aws_iam_role (cluster role + node role)
#   - aws_iam_openid_connect_provider (when var.enable_irsa = true)
#   - aws_kms_key + aws_kms_alias (when var.encryption_secrets = true)

# ---- KMS CMK for K8s Secrets envelope encryption (HIPAA/PCI) ----
resource "aws_kms_key" "eks_secrets" {
  count = var.encryption_secrets ? 1 : 0

  description             = "${var.name_prefix} EKS secrets envelope encryption (HIPAA/PCI)"
  deletion_window_in_days = 30
  enable_key_rotation     = true
  tags                    = var.tags
}

resource "aws_kms_alias" "eks_secrets" {
  count = var.encryption_secrets ? 1 : 0

  name          = "alias/${var.name_prefix}-eks-secrets"
  target_key_id = aws_kms_key.eks_secrets[0].key_id
}

# ---- IAM role: EKS control plane ----
resource "aws_iam_role" "cluster" {
  for_each = var.clusters

  name = "${each.value.name}-cluster-role"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "eks.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
  tags = var.tags
}

resource "aws_iam_role_policy_attachment" "cluster_policy" {
  for_each   = var.clusters
  role       = aws_iam_role.cluster[each.key].name
  policy_arn = "arn:aws:iam::aws:policy/AmazonEKSClusterPolicy"
}

# ---- EKS Cluster ----
resource "aws_eks_cluster" "this" {
  for_each = var.clusters

  name     = each.value.name
  version  = each.value.cluster_version
  role_arn = aws_iam_role.cluster[each.key].arn

  vpc_config {
    subnet_ids              = var.subnet_ids
    endpoint_private_access = true
    endpoint_public_access  = var.endpoint_public_access
    security_group_ids      = []
  }

  # Control plane logging — HIPAA/PCI demand audit + api types.
  enabled_cluster_log_types = var.logging_enabled_types

  # Secrets envelope encryption (HIPAA/PCI).
  dynamic "encryption_config" {
    for_each = var.encryption_secrets ? [1] : []
    content {
      provider {
        key_arn = aws_kms_key.eks_secrets[0].arn
      }
      resources = ["secrets"]
    }
  }

  tags = merge(
    var.tags,
    { Name = each.value.name },
  )

  depends_on = [aws_iam_role_policy_attachment.cluster_policy]
}

# ---- IAM role: managed node group ----
resource "aws_iam_role" "node_group" {
  for_each = var.clusters

  name = "${each.value.name}-node-role"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "ec2.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
  tags = var.tags
}

resource "aws_iam_role_policy_attachment" "node_worker" {
  for_each   = var.clusters
  role       = aws_iam_role.node_group[each.key].name
  policy_arn = "arn:aws:iam::aws:policy/AmazonEKSWorkerNodePolicy"
}

resource "aws_iam_role_policy_attachment" "node_cni" {
  for_each   = var.clusters
  role       = aws_iam_role.node_group[each.key].name
  policy_arn = "arn:aws:iam::aws:policy/AmazonEKS_CNI_Policy"
}

resource "aws_iam_role_policy_attachment" "node_ecr" {
  for_each   = var.clusters
  role       = aws_iam_role.node_group[each.key].name
  policy_arn = "arn:aws:iam::aws:policy/AmazonEC2ContainerRegistryReadOnly"
}

# ---- Managed node groups (one per source nodepool, per cluster) ----
locals {
  flat_node_groups = flatten([
    for ck, c in var.clusters : [
      for ngk, ng in c.node_groups : {
        cluster_key = ck
        ng_key      = ngk
        ng          = ng
      }
    ]
  ])
}

resource "aws_eks_node_group" "this" {
  for_each = { for n in local.flat_node_groups : "${n.cluster_key}__${n.ng_key}" => n }

  cluster_name    = aws_eks_cluster.this[each.value.cluster_key].name
  node_group_name = each.value.ng.name
  node_role_arn   = aws_iam_role.node_group[each.value.cluster_key].arn
  subnet_ids      = var.subnet_ids
  instance_types  = each.value.ng.instance_types
  disk_size       = each.value.ng.disk_size_gb

  scaling_config {
    min_size     = each.value.ng.min_size
    max_size     = each.value.ng.max_size
    desired_size = each.value.ng.desired_size
  }

  tags = merge(
    var.tags,
    { Name = each.value.ng.name },
  )

  depends_on = [
    aws_iam_role_policy_attachment.node_worker,
    aws_iam_role_policy_attachment.node_cni,
    aws_iam_role_policy_attachment.node_ecr,
  ]
}

# ---- OIDC provider for IRSA (HIPAA) ----
data "tls_certificate" "cluster_oidc" {
  for_each = var.enable_irsa ? var.clusters : {}
  url      = aws_eks_cluster.this[each.key].identity[0].oidc[0].issuer
}

resource "aws_iam_openid_connect_provider" "cluster_oidc" {
  for_each = var.enable_irsa ? var.clusters : {}

  url             = aws_eks_cluster.this[each.key].identity[0].oidc[0].issuer
  client_id_list  = ["sts.amazonaws.com"]
  thumbprint_list = [data.tls_certificate.cluster_oidc[each.key].certificates[0].sha1_fingerprint]

  tags = var.tags
}
'''


_VARIABLES_TF = '''# `clusters` is map(any) so callers can supply heterogeneous attrs
# across clusters (different node-group counts, optional fields).
# Implicit schema:
#   name             = string
#   cluster_version  = string   # e.g. "1.31"
#   node_groups      = map(any) # {name, instance_types[], min/max/desired_size, disk_size_gb}
variable "clusters" {
  type        = map(any)
  description = "Map of cluster key -> spec. Schema documented in translator source."
  default     = {}
}

variable "vpc_id" {
  type        = string
  description = "VPC where EKS will deploy (typically reused across all clusters in this root)."
}

variable "subnet_ids" {
  type        = list(string)
  description = "Private subnet IDs for the EKS nodes (multi-AZ recommended)."
  default     = []
}

variable "endpoint_public_access" {
  type        = bool
  default     = true
  description = "Whether the EKS API endpoint is reachable from the public internet. HIPAA/SOC2/PCI: false."
}

variable "encryption_secrets" {
  type        = bool
  default     = false
  description = "Enable envelope encryption of K8s Secrets via a customer-managed KMS CMK. HIPAA/PCI required."
}

variable "logging_enabled_types" {
  type        = list(string)
  default     = []
  description = "Cluster control-plane log types to enable. HIPAA/PCI: [api, audit, authenticator]."
}

variable "enable_irsa" {
  type        = bool
  default     = false
  description = "Provision OIDC provider for IRSA (ServiceAccount → IAM role). HIPAA: true."
}

variable "name_prefix" {
  type    = string
  default = "migrator"
}

variable "tags" {
  type    = map(string)
  default = {}
}
'''


_OUTPUTS_TF = '''output "cluster_endpoints" {
  value = { for k, c in aws_eks_cluster.this : k => c.endpoint }
  description = "Map of cluster key -> API endpoint URL."
}

output "cluster_names" {
  value = { for k, c in aws_eks_cluster.this : k => c.name }
  description = "Map of cluster key -> cluster name."
}

output "oidc_provider_arns" {
  value       = { for k, p in aws_iam_openid_connect_provider.cluster_oidc : k => p.arn }
  description = "Map of cluster key -> OIDC provider ARN (for IRSA role wiring)."
}

output "node_group_arns" {
  value = { for k, ng in aws_eks_node_group.this : k => ng.arn }
  description = "Map of node group key -> ARN."
}
'''


_README = '''# AWS EKS module

Translates GCP `google_container_cluster` (GKE) → AWS EKS. Per cluster:

- `aws_eks_cluster` with private API endpoint (HIPAA/SOC2/PCI default)
- `aws_eks_node_group` (one per source GKE nodepool, managed type)
- `aws_iam_role` for control plane + node group
- `aws_iam_openid_connect_provider` for IRSA when `enable_irsa = true`
- `aws_kms_key` + `aws_kms_alias` for K8s Secrets envelope encryption
  when `encryption_secrets = true`

## GCP→AWS mapping notes

- `gke_cluster_name` → `aws_eks_cluster.name`
- GKE nodepool `machine_type: e2-standard-4` → instance type `m7g.xlarge`
  (Graviton; operator can override per workload). Full map in
  `translate/eks.py::_MACHINE_TYPE_MAP`.
- `master_ipv4_cidr_block` → no direct analog (EKS doesn't expose
  control-plane CIDR). Restrict access via
  `endpoint_public_access_cidrs` if API server must be public-reachable.
- GKE Workload Identity → AWS IRSA. ServiceAccounts annotate role ARN.
- GKE Cloud KMS app-layer secrets encryption → EKS `encryption_config.resources = ["secrets"]`.

## Compliance profile defaults applied

| Profile | endpoint_public_access | encryption_secrets | logging types | IRSA |
|---|---|---|---|---|
| none  | true  (open)        | false      | (none)                                | false |
| hipaa | **false** (private) | **true**   | api, audit, authenticator             | **true** |
| soc2  | **false** (private) | false      | api, audit                            | false |
| pci   | **false** (private) | **true**   | api, audit, authenticator             | false |

## Manual review needed

- **GKE nodepool tags / taints / labels** — translate to EKS node group
  `labels` + `taints` (similar but different syntax).
- **GKE network policies** — convert to AWS Network Policies via
  Calico/Cilium addon (not handled by this module).
- **GKE upgrades** — EKS uses `version` argument for control plane;
  node groups updated via `release_version`. Customer's GKE auto-upgrade
  policy needs translation.
- **GKE PSP / Pod Security** — convert to Pod Security Admission labels
  on the namespace (post-deploy).
'''
