"""GCP google_certificate_manager_certificate → AWS aws_acm_certificate.

Source pattern:

    inputs = {
      certificates = [
        { name, domains = [...], validation_method = "DNS" }
      ]
    }

DNS validation is the recommended pattern for both GCP and AWS — we
emit the cert + the DNS-validation records, and let the operator wire
them to their Route53 zone separately.
"""

from __future__ import annotations

from typing import List

from migrator.results import DiscoveredResource

from .base import DEFAULT_VERSIONS_TF, AWSModuleSpec, Translation


SERVICE_NAME = "acm-certificate"


def translate(resource: DiscoveredResource) -> Translation:
    args = resource.arguments or {}
    notes: List[str] = []

    raw_certs = (
        args.get("certificates")
        or args.get("certificate_configs")
        or args.get("ssl_certificates", {}).get("certificate_ids")
        or []
    )
    if not isinstance(raw_certs, list):
        raw_certs = []

    certs = []
    for src in raw_certs:
        if isinstance(src, dict):
            name = str(src.get("name", "TODO-cert-name"))
            domains = src.get("domains") or src.get("subject_alternative_names") or []
            if not isinstance(domains, list):
                domains = [domains] if isinstance(domains, str) else []
            domains = [str(d) for d in domains]
            primary_domain = domains[0] if domains else "TODO-domain.example.com"
            sans = domains[1:] if len(domains) > 1 else []
        elif isinstance(src, str):
            # Cert ID-only references — no domain info
            name = src
            primary_domain = f"TODO-domain-for-{name}.example.com"
            sans = []
            notes.append(
                f"certificate `{name}` referenced by name only (source had no inline domains); "
                "operator must fill in primary_domain + SANs."
            )
        else:
            continue

        certs.append({
            "name":             name,
            "primary_domain":   primary_domain,
            "subject_alternative_names": sans,
            "validation_method": "DNS",
        })

    if not certs:
        notes.append("No certificate configs detected in source; emitted empty list.")
    else:
        notes.append(f"Emitted {len(certs)} ACM certificate entries with DNS validation.")
        notes.append("DNS validation records: module emits the records as outputs; "
                     "operator wires them to their Route53 zone separately.")

    aws_inputs_hcl = (
        "  # Translated from GCP google_certificate_manager_certificate.\n"
        f"  certificates = {_render_certs(certs)}\n"
    )

    return Translation(
        service_name=SERVICE_NAME,
        aws_inputs_hcl=aws_inputs_hcl,
        notes=notes,
    )


def _render_certs(certs: list) -> str:
    if not certs:
        return "{}"
    lines = ["{"]
    for c in certs:
        key = c["name"].replace("-", "_").replace(".", "_")
        lines.append(f'    "{key}" = {{')
        lines.append(f'      primary_domain     = "{c["primary_domain"]}"')
        if c["subject_alternative_names"]:
            sans = ", ".join(f'"{d}"' for d in c["subject_alternative_names"])
            lines.append(f"      subject_alternative_names = [{sans}]")
        else:
            lines.append("      subject_alternative_names = []")
        lines.append(f'      validation_method  = "{c["validation_method"]}"')
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


_MAIN_TF = '''# AWS ACM Certificate module — emitted by Cloud Lifecycle Intelligence Migrator.

resource "aws_acm_certificate" "this" {
  for_each = var.certificates

  domain_name               = each.value.primary_domain
  subject_alternative_names = each.value.subject_alternative_names
  validation_method         = each.value.validation_method

  lifecycle {
    create_before_destroy = true
  }

  tags = merge(
    var.tags,
    { Name = each.value.primary_domain },
  )
}
'''


_VARIABLES_TF = '''variable "certificates" {
  type = map(object({
    primary_domain            = string
    subject_alternative_names = list(string)
    validation_method         = string  # "DNS" recommended; "EMAIL" supported
  }))
  description = "Map of certificate key -> spec."
  default     = {}
}

variable "tags" {
  type    = map(string)
  default = {}
}
'''


_OUTPUTS_TF = '''output "certificate_arns" {
  value = { for k, c in aws_acm_certificate.this : k => c.arn }
  description = "Map of cert key -> ACM cert ARN."
}

output "validation_records" {
  value = {
    for k, c in aws_acm_certificate.this :
    k => [for o in c.domain_validation_options : {
      name  = o.resource_record_name
      type  = o.resource_record_type
      value = o.resource_record_value
    }]
  }
  description = "DNS validation records to add to your Route 53 zone (one per domain)."
}
'''


_README = '''# AWS ACM Certificate module

Translates GCP `google_certificate_manager_certificate`. Each cert →
one ACM cert with DNS validation enabled.

## Required follow-up: wire DNS validation records

ACM certs go through DNS validation: AWS requires you to publish a
specific CNAME record per domain in your DNS zone. This module emits
those records as the `validation_records` output. Wire them to your
Route 53 zone (or the customer's existing DNS provider) before applying.

If you also use the Migrator-emitted `route53` module, you can wire
both modules together via `aws_route53_record` referencing this module's
output.

## Differences from GCP Certificate Manager

- ACM certs are scoped to a region — to use the same cert in multiple
  regions, provision one ACM cert per region.
- ACM-issued certs are free; private cert authorities (Private CA)
  cost separately.
'''
