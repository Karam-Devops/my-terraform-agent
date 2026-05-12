"""GCP google_compute_forwarding_rule / Cloud Load Balancer → AWS ALB.

GCP load balancers are organized in a multi-layer stack:
  google_compute_global_forwarding_rule (frontend) →
  google_compute_target_https_proxy (TLS termination) →
  google_compute_url_map (routing) →
  google_compute_backend_service (backends + health checks)

AWS Application Load Balancer (ALB) compresses this into ~3 resources:
  aws_lb (load balancer) →
  aws_lb_listener (port + TLS cert) →
  aws_lb_target_group (backends + health checks) + aws_lb_listener_rule

Both are L7 (HTTP/HTTPS) load balancers with similar feature sets:
  - SSL/TLS termination
  - Path-based + host-based routing
  - Health checks against backends
  - WAF / SSL policy attachment
  - Access logging to S3

This translator handles the common case (single HTTPS listener + one
target group). Multi-listener / multi-rule configs are surfaced as
TODO comments for operator review.

Source patterns (customer's terragrunt inputs, varies):

    # Pattern A: top-level lb_config
    inputs = {
      lb_config = {
        name            = "api-ext-lb"
        backend_service = "..."
        ssl_cert_id     = "..."
      }
    }

    # Pattern B: forwarding_rules list
    inputs = {
      forwarding_rules = [
        { name = "...", target_proxy = "...", ssl_certificate = "..." }
      ]
    }
"""

from __future__ import annotations

from typing import List

from migrator.results import DiscoveredResource

from .base import DEFAULT_VERSIONS_TF, AWSModuleSpec, Translation


SERVICE_NAME = "alb"


def translate(
    resource: DiscoveredResource,
    *,
    compliance_profile: str = "none",
) -> Translation:
    """Translate GCP global forwarding rule / LB → AWS Application Load Balancer.

    Compliance profile defaults:
      - access_logs_enabled: forced True under HIPAA/SOC2/PCI
      - drop_invalid_header_fields: forced True under HIPAA/PCI
      - min_tls_version: forced TLSv1.2_2021 under HIPAA/SOC2/PCI
    """
    from migrator.translate.compliance_profiles import get_defaults
    _profile_defaults = get_defaults(compliance_profile, "alb")

    args = resource.arguments or {}
    notes: List[str] = []

    # Extract LB specs from various source shapes.
    lb_specs = []

    # Pattern A: top-level lb_config
    if "lb_config" in args and isinstance(args["lb_config"], dict):
        lb_specs.append(_normalize_lb_spec(args["lb_config"], resource.name))

    # Pattern B: forwarding_rules list
    fw_rules = args.get("forwarding_rules") or args.get("lb_configs") or []
    if isinstance(fw_rules, list):
        for rule in fw_rules:
            if isinstance(rule, dict):
                lb_specs.append(_normalize_lb_spec(rule, resource.name))

    # Pattern C: top-level inputs (single LB)
    if not lb_specs:
        if "name" in args or "backend_service" in args:
            lb_specs.append(_normalize_lb_spec(args, resource.name))

    if not lb_specs:
        # Fallback: single placeholder
        lb_specs.append({
            "name":               args.get("name", resource.name),
            "internal":           False,
            "ssl_certificate_id": "TODO-acm-cert-arn",
            "backend_port":       80,
            "health_check_path":  "/health",
        })
        notes.append("Could not detect lb_config / forwarding_rules in inputs; emitted single ALB placeholder.")

    # Compliance-profile derived attrs
    access_logs_enabled = bool(_profile_defaults.get("access_logs_enabled", False))
    drop_invalid_headers = bool(_profile_defaults.get("drop_invalid_header_fields", False))
    min_tls_version = _profile_defaults.get("min_tls_version", "TLSv1.2_2017")

    notes.insert(0, f"Emitted {len(lb_specs)} Application Load Balancer(s).")
    notes.append("GCP Global HTTP(S) LB ← anycast IP; AWS ALB ← regional. For multi-region "
                 "active-active, fronting with CloudFront + Global Accelerator may be needed.")
    notes.append("Backend wiring (target groups → EC2 / EKS / ECS) is left as a TODO per LB. "
                 "Operator decides target type (instance / ip / lambda) per backend service.")
    if compliance_profile and compliance_profile != "none":
        hardened = []
        if access_logs_enabled:    hardened.append("access_logs_to_s3")
        if drop_invalid_headers:   hardened.append("drop_invalid_header_fields")
        if min_tls_version != "TLSv1.2_2017": hardened.append(f"min_tls_version={min_tls_version}")
        if hardened:
            notes.append(
                f"compliance profile '{compliance_profile.upper()}' applied — "
                f"defaults forced on: {', '.join(hardened)}"
            )

    aws_inputs_hcl = (
        "  # Translated from GCP Cloud Load Balancer → AWS ALB.\n"
        f"  load_balancers = {_render_lbs(lb_specs)}\n"
        f"\n"
        f"  # Compliance-profile-driven attrs (only emitted when set):\n"
    )
    if access_logs_enabled:
        aws_inputs_hcl += "  access_logs_enabled        = true                  # compliance profile\n"
    if drop_invalid_headers:
        aws_inputs_hcl += "  drop_invalid_header_fields = true                  # compliance profile\n"
    if min_tls_version and min_tls_version != "TLSv1.2_2017":
        aws_inputs_hcl += f'  min_tls_version            = "{min_tls_version}"   # compliance profile\n'

    aws_inputs_hcl += (
        "\n"
        "  # TODO: wire to networking module outputs\n"
        '  vpc_id     = "TODO-vpc-id"\n'
        "  subnet_ids = []  # public subnets for internet-facing; private for internal\n"
    )

    return Translation(
        service_name=SERVICE_NAME,
        aws_inputs_hcl=aws_inputs_hcl,
        notes=notes,
    )


def _normalize_lb_spec(src: dict, fallback_name: str) -> dict:
    """Normalize a varied source LB config into our internal spec shape."""
    return {
        "name":               str(src.get("name") or src.get("lb_name") or fallback_name),
        "internal":           bool(src.get("internal") or src.get("is_internal", False)),
        "ssl_certificate_id": str(
            src.get("ssl_cert_id")
            or src.get("ssl_certificate")
            or src.get("certificate_arn")
            or "TODO-acm-cert-arn"
        ),
        "backend_port":       int(src.get("backend_port") or src.get("port", 80) or 80),
        "health_check_path":  str(src.get("health_check_path") or src.get("health_path", "/health")),
    }


def _render_lbs(specs: list) -> str:
    if not specs:
        return "{}"
    lines = ["{"]
    for s in specs:
        key = s["name"].replace("-", "_").replace(".", "_")
        lines.append(f'    "{key}" = {{')
        lines.append(f'      name                = "{s["name"]}"')
        lines.append(f'      internal            = {str(s["internal"]).lower()}')
        lines.append(f'      ssl_certificate_arn = "{s["ssl_certificate_id"]}"')
        lines.append(f'      backend_port        = {s["backend_port"]}')
        lines.append(f'      health_check_path   = "{s["health_check_path"]}"')
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


_MAIN_TF = '''# AWS ALB module — emitted by Cloud Lifecycle Intelligence Migrator.
# Translates GCP Cloud Load Balancer (global HTTP(S) LB) → ALB.
#
# Per load balancer:
#   - aws_lb              (the ALB itself)
#   - aws_lb_listener     (HTTPS on 443, HTTP redirect on 80)
#   - aws_lb_target_group (backends; placeholder, operator wires actual targets)
#   - aws_security_group  (allow 443 ingress; operator adjusts source CIDRs)

# ---- S3 bucket for access logs (HIPAA/SOC2/PCI) ----
resource "aws_s3_bucket" "alb_logs" {
  count = var.access_logs_enabled && var.access_logs_bucket == "" ? 1 : 0

  bucket        = "${var.name_prefix}-alb-access-logs"
  force_destroy = false
  tags          = var.tags
}

resource "aws_s3_bucket_policy" "alb_logs" {
  count = var.access_logs_enabled && var.access_logs_bucket == "" ? 1 : 0

  bucket = aws_s3_bucket.alb_logs[0].id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Principal = { Service = "logdelivery.elasticloadbalancing.amazonaws.com" }
      Action = "s3:PutObject"
      Resource = "${aws_s3_bucket.alb_logs[0].arn}/*"
    }]
  })
}

locals {
  access_logs_bucket_id = var.access_logs_bucket != "" ? var.access_logs_bucket : (
    var.access_logs_enabled ? aws_s3_bucket.alb_logs[0].id : ""
  )
}

# ---- Security group: allow inbound 443 (and optional 80) ----
resource "aws_security_group" "alb" {
  for_each = var.load_balancers

  name        = "${each.value.name}-alb-sg"
  description = "Security group for ${each.value.name} ALB"
  vpc_id      = var.vpc_id

  ingress {
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = var.allowed_cidrs
  }

  ingress {
    from_port   = 80
    to_port     = 80
    protocol    = "tcp"
    cidr_blocks = var.allowed_cidrs
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = var.tags
}

# ---- ALB itself ----
resource "aws_lb" "this" {
  for_each = var.load_balancers

  name               = each.value.name
  internal           = each.value.internal
  load_balancer_type = "application"
  subnets            = var.subnet_ids
  security_groups    = [aws_security_group.alb[each.key].id]

  # Compliance-profile-driven attrs.
  drop_invalid_header_fields = var.drop_invalid_header_fields

  dynamic "access_logs" {
    for_each = var.access_logs_enabled ? [1] : []
    content {
      bucket  = local.access_logs_bucket_id
      prefix  = "alb/${each.value.name}"
      enabled = true
    }
  }

  tags = merge(var.tags, { Name = each.value.name })
}

# ---- Target group (placeholder; operator wires real backends) ----
resource "aws_lb_target_group" "this" {
  for_each = var.load_balancers

  name        = "${each.value.name}-tg"
  port        = each.value.backend_port
  protocol    = "HTTP"
  target_type = "instance"
  vpc_id      = var.vpc_id

  health_check {
    enabled             = true
    path                = each.value.health_check_path
    matcher             = "200"
    interval            = 30
    timeout             = 5
    healthy_threshold   = 3
    unhealthy_threshold = 3
  }

  tags = var.tags
}

# ---- HTTPS listener with TLS termination ----
resource "aws_lb_listener" "https" {
  for_each = var.load_balancers

  load_balancer_arn = aws_lb.this[each.key].arn
  port              = 443
  protocol          = "HTTPS"
  ssl_policy        = var.min_tls_version == "TLSv1.2_2021" ? "ELBSecurityPolicy-TLS13-1-2-2021-06" : "ELBSecurityPolicy-2016-08"
  certificate_arn   = each.value.ssl_certificate_arn

  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.this[each.key].arn
  }

  tags = var.tags
}

# ---- HTTP listener (redirects to HTTPS) ----
resource "aws_lb_listener" "http_redirect" {
  for_each = var.load_balancers

  load_balancer_arn = aws_lb.this[each.key].arn
  port              = 80
  protocol          = "HTTP"

  default_action {
    type = "redirect"

    redirect {
      port        = "443"
      protocol    = "HTTPS"
      status_code = "HTTP_301"
    }
  }

  tags = var.tags
}
'''


_VARIABLES_TF = '''variable "load_balancers" {
  type        = map(any)
  description = <<EOT
Map of LB key -> spec. Required attrs:
  name                = string
  internal            = bool
  ssl_certificate_arn = string
  backend_port        = number
  health_check_path   = string
EOT
  default     = {}
}

variable "vpc_id" {
  type        = string
  description = "VPC ID where the ALB(s) will live."
}

variable "subnet_ids" {
  type        = list(string)
  description = "Subnet IDs for the ALB. Use public subnets for internet-facing LBs; private for internal."
  default     = []
}

variable "allowed_cidrs" {
  type        = list(string)
  description = "Source CIDRs allowed to reach the ALB on 80/443. Tighten for internal-only LBs."
  default     = ["0.0.0.0/0"]
}

variable "access_logs_enabled" {
  type        = bool
  default     = false
  description = "When true, enable ALB access logging to S3. HIPAA/SOC2/PCI: true."
}

variable "access_logs_bucket" {
  type        = string
  default     = ""
  description = "Existing S3 bucket name for access logs. Empty = module creates one."
}

variable "drop_invalid_header_fields" {
  type        = bool
  default     = false
  description = "Drop HTTP requests with malformed headers. HIPAA/PCI: true."
}

variable "min_tls_version" {
  type        = string
  default     = "TLSv1.2_2017"
  description = "Minimum TLS version. HIPAA/SOC2/PCI: TLSv1.2_2021 (uses ELBSecurityPolicy-TLS13-1-2-2021-06)."
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


_OUTPUTS_TF = '''output "alb_arns" {
  value       = { for k, lb in aws_lb.this : k => lb.arn }
  description = "Map of LB key -> ARN."
}

output "alb_dns_names" {
  value       = { for k, lb in aws_lb.this : k => lb.dns_name }
  description = "Map of LB key -> DNS name (route Route53 records here)."
}

output "target_group_arns" {
  value       = { for k, tg in aws_lb_target_group.this : k => tg.arn }
  description = "Map of LB key -> target group ARN (wire EC2/EKS/ECS targets to these)."
}
'''


_README = '''# AWS Application Load Balancer module

Translates GCP Cloud Load Balancer (global HTTP(S) LB) → AWS ALB. Per LB:

- `aws_lb` (Application Load Balancer)
- `aws_lb_listener` (HTTPS on 443 with TLS termination)
- `aws_lb_listener` (HTTP on 80 redirecting to HTTPS)
- `aws_lb_target_group` (backend pool; operator wires actual targets)
- `aws_security_group` (ingress 80/443 from var.allowed_cidrs)

## GCP→AWS architectural shift

| GCP global HTTP(S) LB              | AWS ALB                          |
|------------------------------------|----------------------------------|
| Anycast IP, global edge            | Regional (one per region)        |
| google_compute_url_map for routing | aws_lb_listener_rule per route   |
| google_compute_backend_service     | aws_lb_target_group              |
| SSL cert from cert-manager         | ACM cert (regional)              |

For multi-region active-active, front the ALBs with CloudFront +
Global Accelerator.

## Compliance profile defaults

| Profile | access_logs | drop_invalid_headers | min_tls_version    |
|---------|-------------|----------------------|--------------------|
| none    | false       | false                | TLSv1.2_2017       |
| hipaa   | **true**    | **true**             | **TLSv1.2_2021**   |
| soc2    | **true**    | false                | **TLSv1.2_2021**   |
| pci     | **true**    | **true**             | **TLSv1.2_2021**   |

## Manual review needed

- **Target wiring** — module emits empty target groups. Operator
  attaches `aws_lb_target_group_attachment` for EC2 instances OR uses
  `aws_eks_pod_identity_association` for EKS pod-targeted routing OR
  registers ECS service via `aws_ecs_service.load_balancer`.
- **Multi-path routing** — `url_map` in GCP can have many path rules.
  Translator emits a single default-forward rule; operator adds
  `aws_lb_listener_rule` resources for path-based routing.
- **WAF attachment** — `aws_wafv2_web_acl_association` separately
  (see waf module).
- **Route53 alias** — point your DNS at the ALB DNS name via the
  route53-zone module.
'''
