"""GCP google_artifact_registry_repository → AWS aws_ecr_repository.

Source pattern:

    inputs = {
      project_id = ...
      repositories = {
        "docker-repo" = {
          format       = "DOCKER"
          description  = "..."
          mode         = "STANDARD_REPOSITORY"
        },
        "helm-repo" = {
          format = "HELM"
        }
      }
    }

ECR is Docker-only. HELM repos translate but with a note (AWS ECR
supports OCI Helm charts, but the API is Docker registry semantics).
"""

from __future__ import annotations

from typing import List

from migrator.results import DiscoveredResource

from .base import DEFAULT_VERSIONS_TF, AWSModuleSpec, Translation


SERVICE_NAME = "ecr-repository"


def translate(resource: DiscoveredResource) -> Translation:
    args = resource.arguments or {}
    notes: List[str] = []

    raw_repos = args.get("repositories") or args.get("repository_config") or {}
    if isinstance(raw_repos, list):
        # Some shapes use a list of dicts
        raw_repos = {r.get("name", f"repo{i}"): r for i, r in enumerate(raw_repos) if isinstance(r, dict)}
    if not isinstance(raw_repos, dict):
        raw_repos = {}

    repos = []
    for key, src in raw_repos.items():
        if not isinstance(src, dict):
            src = {}
        name = str(src.get("name", key))
        gcp_format = str(src.get("format", "DOCKER")).upper()
        description = str(src.get("description", f"Migrated from GCP Artifact Registry: {name}"))

        if gcp_format == "HELM":
            notes.append(
                f"repository `{name}` is HELM format. AWS ECR supports OCI Helm charts "
                "but uses Docker registry semantics for push/pull — verify your Helm clients are OCI-aware."
            )
        elif gcp_format not in ("DOCKER", "OCI"):
            notes.append(
                f"repository `{name}` has unmapped format `{gcp_format}` — defaulted to standard ECR; "
                "AWS may need a different service (Maven/npm registries → CodeArtifact)."
            )

        repos.append({
            "name":         name,
            "description":  description,
            "scan_on_push": True,
            "image_mutability": "IMMUTABLE",
        })

    if not repos:
        notes.append("No repositories detected in source; emitted empty map.")
    else:
        notes.append(f"Emitted {len(repos)} ECR repository entries with scan-on-push + immutable tags.")

    aws_inputs_hcl = (
        "  # Translated from GCP google_artifact_registry_repository.\n"
        f"  repositories = {_render_repos(repos)}\n"
    )

    return Translation(
        service_name=SERVICE_NAME,
        aws_inputs_hcl=aws_inputs_hcl,
        notes=notes,
    )


def _render_repos(repos: list) -> str:
    if not repos:
        return "{}"
    lines = ["{"]
    for r in repos:
        key = r["name"].replace("-", "_").replace(".", "_").replace("/", "_")
        lines.append(f'    "{key}" = {{')
        lines.append(f'      name             = "{r["name"]}"')
        lines.append(f'      description      = "{r["description"]}"')
        lines.append(f'      scan_on_push     = {str(r["scan_on_push"]).lower()}')
        lines.append(f'      image_mutability = "{r["image_mutability"]}"')
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


_MAIN_TF = '''# AWS ECR module — emitted by Cloud Lifecycle Intelligence Migrator.

resource "aws_ecr_repository" "this" {
  for_each = var.repositories

  name                 = each.value.name
  image_tag_mutability = each.value.image_mutability

  image_scanning_configuration {
    scan_on_push = each.value.scan_on_push
  }

  encryption_configuration {
    encryption_type = "AES256"
  }

  tags = merge(
    var.tags,
    { Name = each.value.name },
  )
}

resource "aws_ecr_lifecycle_policy" "this" {
  for_each   = var.repositories
  repository = aws_ecr_repository.this[each.key].name

  policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "Keep last 100 images"
      selection = {
        tagStatus   = "any"
        countType   = "imageCountMoreThan"
        countNumber = 100
      }
      action = { type = "expire" }
    }]
  })
}
'''


_VARIABLES_TF = '''variable "repositories" {
  type = map(object({
    name             = string
    description      = string
    scan_on_push     = bool
    image_mutability = string  # IMMUTABLE | MUTABLE
  }))
  description = "Map of repo key -> spec."
  default     = {}
}

variable "tags" {
  type    = map(string)
  default = {}
}
'''


_OUTPUTS_TF = '''output "repository_urls" {
  value = { for k, r in aws_ecr_repository.this : k => r.repository_url }
  description = "Map of repo key -> ECR repository URL (use as docker push target)."
}

output "repository_arns" {
  value = { for k, r in aws_ecr_repository.this : k => r.arn }
  description = "Map of repo key -> ARN."
}
'''


_README = '''# AWS ECR Repository module

Translates GCP `google_artifact_registry_repository` for Docker-format
repos. Each entry → one ECR repository with:
- AES256 encryption-at-rest
- Image scanning on push (Amazon Inspector)
- Immutable tags (best practice — prevents tag rewriting attacks)
- Lifecycle policy keeping the last 100 images

## Post-deploy: image migration

Use `migration_helpers/05-artifact-registry-to-ecr.sh` to mirror images
from GCP Artifact Registry to AWS ECR. The script authenticates against
both, lists images by tag, and re-pushes to the ECR repo.
'''
