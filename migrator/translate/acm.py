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

    # Source-shape detection — five customer patterns observed:
    #   1. `certificates`         list-of-dicts with `{name, domains}`
    #   2. `certificate_configs`  alias for #1
    #   3. `ssl_certificates.certificate_ids`  cert-id refs only
    #   4. `classic_certificates` (DH customer) dict-of-dicts where each
    #      entry holds Secret Manager refs but NO domains:
    #        {
    #          "deephealth-tls-certificate-dec-2026" = {
    #            description        = "..."
    #            certificate_secret = "secret-name-in-secret-manager"
    #            private_key_secret = "key-secret-name"
    #          }
    #        }
    #      For #4 we emit the cert NAMES with placeholder domains + a
    #      loud note that the operator must (a) migrate the cert+key
    #      from Secret Manager → AWS Secrets Manager and (b) fill in
    #      the real domain(s) the cert covers (or use imported cert
    #      material).
    raw_certs = (
        args.get("certificates")
        or args.get("certificate_configs")
        or args.get("classic_certificates")
        or args.get("ssl_certificates", {}).get("certificate_ids")
        or []
    )

    # Normalize the four source shapes into a uniform iteration:
    #   * dict-of-dicts  → list of (key, value-dict)
    #   * list-of-dicts  → list of (None, value-dict)   ← keep order
    #   * list-of-strs   → list of (str, None)          ← bare id refs
    iter_pairs: list = []
    if isinstance(raw_certs, dict):
        for k, v in raw_certs.items():
            if isinstance(v, dict):
                iter_pairs.append((str(k), v))
    elif isinstance(raw_certs, list):
        for entry in raw_certs:
            if isinstance(entry, dict):
                iter_pairs.append((None, entry))
            elif isinstance(entry, str):
                iter_pairs.append((entry, None))

    certs = []
    for map_key, src in iter_pairs:
        if isinstance(src, dict):
            # Prefer explicit `name`; fall back to the source map key
            # (DH's classic_certificates pattern uses the key as the id).
            name = str(src.get("name") or map_key or "TODO-cert-name")
            domains = src.get("domains") or src.get("subject_alternative_names") or []
            if not isinstance(domains, list):
                domains = [domains] if isinstance(domains, str) else []
            domains = [str(d) for d in domains]
            if domains:
                primary_domain = domains[0]
                sans = domains[1:] if len(domains) > 1 else []
            else:
                # No inline domain info — common with Secret-Manager-backed
                # "classic" certs. Surface this to the operator instead of
                # silently emitting an empty domains list (which is what
                # was happening before the fix).
                primary_domain = f"TODO-domain-for-{name}.example.com"
                sans = []
                # If this is a classic-cert pattern (Secret-Manager refs),
                # include those refs in the note so the operator knows
                # exactly which secrets to migrate.
                secret_hint = ""
                if src.get("certificate_secret") or src.get("private_key_secret"):
                    secret_hint = (
                        f" Source uses Secret Manager refs: "
                        f"cert={src.get('certificate_secret', '?')}, "
                        f"key={src.get('private_key_secret', '?')}. "
                        f"Migrate these to AWS Secrets Manager and import "
                        f"the cert material via aws_acm_certificate.private_key + "
                        f"certificate_body, OR re-issue via ACM DNS validation."
                    )
                notes.append(
                    f"certificate `{name}` has no inline domains in source — "
                    f"operator must fill in primary_domain + SANs.{secret_hint}"
                )
        else:
            # Bare cert-id reference (list-of-strings shape)
            name = map_key or "TODO-cert-name"
            primary_domain = f"TODO-domain-for-{name}.example.com"
            sans = []
            notes.append(
                f"certificate `{name}` referenced by name only (source had no inline domains); "
                "operator must fill in primary_domain + SANs."
            )

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
