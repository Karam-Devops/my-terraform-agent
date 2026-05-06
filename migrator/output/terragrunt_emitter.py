"""Emit a scaffolded AWS Terragrunt repo mirroring the source structure.

For each leaf source stack (one per source ``terragrunt.hcl``) we write
an equivalent target ``terragrunt.hcl`` under ``<output_dir>/target/``
preserving the source's ``live/<env>/<region>/<stack>/`` directory
hierarchy. Each emitted file contains:

  * `include "root"` — pulls in the synthesized AWS root config
  * `terraform { source = ... }` — placeholder pointing at where the
    operator's AWS module library should live, named after the
    inferred AWS resource type
  * `inputs = { ... }` — source GCP inputs commented inline as a
    translation aid; AWS-equivalent input keys listed as TODO

Plus a synthesized AWS root ``terragrunt.hcl`` with S3 + DynamoDB
backend, AWS provider generate block, and shared locals.

Design phase deferred: full per-resource HCL translation is filled in
post-demo (see phase7_migrator_strategy memory). This emitter delivers
the scaffolded *structure* — operator review against the migration
guide fills in the per-resource HCL.
"""

from __future__ import annotations

import os
from typing import Dict, List, Optional

from migrator.results import ConfidenceFinding, DiscoveredResource


# Default AWS region target — operator overrides via the Clarify form
# in v2 (Streamlit page).
_DEFAULT_AWS_REGION = "us-east-1"

# Placeholder GitHub URL for the AWS module library — operator updates
# to point at their own module repo.
_PLACEHOLDER_AWS_MODULE_BASE = "git::https://github.com/<your-org>/aws-modules.git"


def emit_terragrunt_skeleton(
    *,
    output_dir: str,
    repo_path: str,
    target_cloud: str,
    resources: List[DiscoveredResource],
    confidence: List[ConfidenceFinding],
    aws_region: Optional[str] = None,
) -> List[str]:
    """Write the AWS Terragrunt skeleton under <output_dir>/target/.

    Returns the list of absolute paths written.
    """
    if target_cloud.lower() != "aws":
        return []

    target_root = os.path.join(output_dir, "target")
    os.makedirs(target_root, exist_ok=True)

    aws_region = aws_region or _DEFAULT_AWS_REGION
    confidence_by_addr = {c.resource_address: c for c in confidence}

    written: List[str] = []

    # ---- 1. AWS root terragrunt.hcl ----
    root_path = os.path.join(target_root, "terragrunt.hcl")
    _write_text(root_path, _render_root_terragrunt(aws_region))
    written.append(root_path)

    # ---- 2. _common/ shared locals + tags ----
    common_dir = os.path.join(target_root, "_common")
    os.makedirs(common_dir, exist_ok=True)
    account_path = os.path.join(common_dir, "account.hcl")
    _write_text(account_path, _render_account_hcl())
    written.append(account_path)

    tags_path = os.path.join(common_dir, "tags.hcl")
    _write_text(tags_path, _render_tags_hcl())
    written.append(tags_path)

    # ---- 3. Per-stack terragrunt.hcl ----
    # Group resources by their source module_path. In Terragrunt mode
    # there's exactly one resource per leaf stack; in vanilla TF there
    # can be many — we emit one stack file per source module path.
    stacks: Dict[str, List[DiscoveredResource]] = {}
    for r in resources:
        stacks.setdefault(r.module_path, []).append(r)

    for module_path, stack_resources in sorted(stacks.items()):
        # Mirror the source's directory structure under target/.
        # Skip the root-level path "." (which means file is at repo root) —
        # we already wrote target/terragrunt.hcl above.
        if module_path in (".", ""):
            continue

        target_stack_dir = os.path.join(target_root, module_path)
        os.makedirs(target_stack_dir, exist_ok=True)

        target_stack_path = os.path.join(target_stack_dir, "terragrunt.hcl")
        rep = stack_resources[0]
        rep_conf = confidence_by_addr.get(rep.address)
        _write_text(
            target_stack_path,
            _render_stack_terragrunt(
                module_path=module_path,
                resources=stack_resources,
                confidence=rep_conf,
            ),
        )
        written.append(target_stack_path)

    # ---- 4. Top-level README inside target/ ----
    readme_path = os.path.join(target_root, "README.md")
    _write_text(readme_path, _render_target_readme(
        repo_path=repo_path,
        aws_region=aws_region,
        stack_count=len(stacks),
    ))
    written.append(readme_path)

    return written


# -----------------------------------------------------------------
# Templates
# -----------------------------------------------------------------

def _render_root_terragrunt(aws_region: str) -> str:
    return (
        "# AWS Terragrunt root — synthesized by Cloud Lifecycle Intelligence Migrator.\n"
        "# Replace placeholder values (account_id, bucket name, DynamoDB table)\n"
        "# with your real AWS landing-zone configuration before running\n"
        "# `terragrunt run-all init`.\n\n"
        "locals {\n"
        '  account = read_terragrunt_config(find_in_parent_folders("account.hcl"))\n'
        '  tags    = read_terragrunt_config(find_in_parent_folders("tags.hcl"))\n'
        '  env     = read_terragrunt_config(find_in_parent_folders("env.hcl"))\n\n'
        "  account_id  = local.account.locals.aws_account_id\n"
        "  environment = local.env.locals.environment\n"
        f'  region      = "{aws_region}"\n'
        "}\n\n"
        "remote_state {\n"
        '  backend = "s3"\n'
        "  generate = {\n"
        '    path      = "backend.tf"\n'
        '    if_exists = "overwrite_terragrunt"\n'
        "  }\n"
        "  config = {\n"
        '    bucket         = "${local.account_id}-tfstate"\n'
        '    key            = "${local.environment}/${path_relative_to_include()}/terraform.tfstate"\n'
        "    region         = local.region\n"
        "    encrypt        = true\n"
        '    dynamodb_table = "tfstate-lock"\n'
        "  }\n"
        "}\n\n"
        'generate "provider" {\n'
        '  path      = "provider.tf"\n'
        '  if_exists = "overwrite_terragrunt"\n'
        "  contents  = <<EOF\n"
        'provider "aws" {\n'
        '  region = "${local.region}"\n\n'
        "  default_tags {\n"
        "    tags = ${jsonencode(local.tags.locals.labels)}\n"
        "  }\n"
        "}\n"
        "EOF\n"
        "}\n\n"
        'generate "versions" {\n'
        '  path      = "versions_override.tf"\n'
        '  if_exists = "overwrite_terragrunt"\n'
        "  contents  = <<EOF\n"
        "terraform {\n"
        '  required_version = ">= 1.5.0, < 2.0.0"\n'
        "  required_providers {\n"
        "    aws = {\n"
        '      source  = "hashicorp/aws"\n'
        '      version = "~> 5.20"\n'
        "    }\n"
        "  }\n"
        "}\n"
        "EOF\n"
        "}\n\n"
        "inputs = {\n"
        "  aws_account_id = local.account.locals.aws_account_id\n"
        "  environment    = local.environment\n"
        "  region         = local.region\n"
        "  labels         = local.tags.locals.labels\n"
        "}\n"
    )


def _render_account_hcl() -> str:
    return (
        "# AWS account-wide locals.\n"
        "# Read by root terragrunt.hcl via\n"
        '# read_terragrunt_config(find_in_parent_folders("account.hcl")).\n'
        "# Replace placeholders with your real values before deploy.\n\n"
        "locals {\n"
        '  aws_account_id  = "REPLACE-WITH-AWS-ACCOUNT-ID"\n'
        '  org_id          = "REPLACE-WITH-AWS-ORG-ID"\n'
        '  identity_center_arn = "REPLACE-WITH-IDENTITY-CENTER-ARN"\n'
        "}\n"
    )


def _render_tags_hcl() -> str:
    return (
        "# Org-wide tags applied to every AWS resource that supports them.\n"
        "# Mirrors the customer's existing GCP labels with AWS-conventional keys.\n\n"
        "locals {\n"
        "  labels = {\n"
        '    managed-by  = "terraform"\n'
        '    cost-center = "platform"\n'
        '    owner       = "platform-team"\n'
        '    compliance  = "internal"\n'
        '    repo        = "REPLACE-WITH-AWS-REPO-NAME"\n'
        "  }\n"
        "}\n"
    )


def _render_stack_terragrunt(
    *,
    module_path: str,
    resources: List[DiscoveredResource],
    confidence: Optional[ConfidenceFinding],
) -> str:
    """Render one stack's AWS terragrunt.hcl.

    The header captures the source GCP context (which module the stack
    came from, what we inferred for AWS, the confidence band). The
    `terraform { source }` points at a placeholder AWS module path
    derived from the inferred AWS resource type. The `inputs` block
    surfaces the source GCP inputs as comments so the operator has the
    full migration context inline while editing.
    """
    rep = resources[0]
    aws_eq = (
        confidence.aws_equivalent if confidence and confidence.aws_equivalent
        else "MANUAL_REVIEW"
    )
    band = confidence.band if confidence else "UNKNOWN"
    score = confidence.score_pct if confidence else 0
    reason = confidence.reason if confidence else "(no confidence rating available)"

    # AWS module path slug derived from the AWS equivalent (strip the
    # `aws_` prefix and replace _ with -). E.g. aws_eks_cluster -> eks-cluster.
    aws_module_slug = (
        aws_eq.removeprefix("aws_").replace("_", "-")
        if aws_eq != "MANUAL_REVIEW" else "MANUAL-REVIEW"
    )

    # Source URL of the original Terragrunt module reference (when
    # available — only Terragrunt-mode resources carry this).
    src_url = ""
    for r in resources:
        u = r.arguments.get("_terragrunt_source") if isinstance(r.arguments, dict) else None
        if isinstance(u, str) and u:
            src_url = u
            break

    lines: List[str] = []
    lines.append(f"# Source GCP module: {module_path}")
    if src_url:
        lines.append(f"# Source Terragrunt source: {src_url}")
    lines.append(f"# Inferred AWS equivalent: {aws_eq}")
    lines.append(f"# Confidence: {band} ({score}%)")
    lines.append(f"# Reason: {reason}")
    if confidence and confidence.notes:
        lines.append("# Notes:")
        for note in confidence.notes:
            lines.append(f"#   - {note}")
    lines.append("")
    lines.append('include "root" {')
    lines.append("  path = find_in_parent_folders()")
    lines.append("}")
    lines.append("")

    if aws_eq == "MANUAL_REVIEW":
        lines.append("# ⚠️ MANUAL REVIEW REQUIRED: no direct AWS equivalent.")
        lines.append("# This stack needs an architectural decision before it can be wired up.")
        lines.append("# Comment-out or remove this terragrunt.hcl once the operator confirms")
        lines.append("# the appropriate AWS service (or that this stack is being retired).")
        lines.append("")
        lines.append("# terraform {")
        lines.append(f'#   source = "{_PLACEHOLDER_AWS_MODULE_BASE}//<TBD-aws-service>?ref=v1.0.0"')
        lines.append("# }")
    else:
        lines.append("terraform {")
        lines.append(f'  source = "{_PLACEHOLDER_AWS_MODULE_BASE}//{aws_module_slug}?ref=v1.0.0"')
        lines.append("}")
    lines.append("")

    # Source inputs (commented as translation reference).
    src_inputs = _collect_source_inputs(resources)
    if src_inputs:
        lines.append("# ---- Source GCP inputs (for translation reference) ----")
        for k, v in sorted(src_inputs.items()):
            if k.startswith("_"):
                continue
            v_str = _stringify(v)
            # Truncate long values so the comment block stays readable.
            if len(v_str) > 80:
                v_str = v_str[:77] + "..."
            lines.append(f"# {k} = {v_str}")
        lines.append("")

    lines.append("inputs = {")
    lines.append("  # TODO: review against the source inputs above and the migration guide.")
    lines.append('  # Run `terragrunt plan` after filling in real values.')
    lines.append("}")

    return "\n".join(lines) + "\n"


def _render_target_readme(
    *,
    repo_path: str,
    aws_region: str,
    stack_count: int,
) -> str:
    return (
        "# AWS Terragrunt skeleton\n\n"
        "Generated by **Cloud Lifecycle Intelligence — Migrator engine** from\n"
        f"`{os.path.abspath(repo_path)}`.\n\n"
        f"- **Stacks emitted:** {stack_count}\n"
        f"- **Default AWS region:** `{aws_region}`\n\n"
        "## What's here\n\n"
        "Every leaf stack from your source GCP Terragrunt repo has a target\n"
        "`terragrunt.hcl` here at the same relative path. Each stub:\n\n"
        "- `include \"root\"` — pulls in `target/terragrunt.hcl` (the AWS root\n"
        "  config: S3 backend, DynamoDB lock, AWS provider generate block).\n"
        "- `terraform { source = ... }` — placeholder URL pointing at where\n"
        "  your AWS module library should live (per inferred AWS resource type).\n"
        "- Source GCP inputs as comments — translation reference.\n"
        "- Empty `inputs = {}` block — operator fills in AWS-equivalent values.\n\n"
        "## Before deploy\n\n"
        "1. Review `MIGRATION_GUIDE.md` (one directory up) for the dependency-\n"
        "   ordered deploy sequence + per-resource confidence ratings.\n"
        "2. Update `_common/account.hcl` with your real AWS account ID, org ID, etc.\n"
        "3. Update `_common/tags.hcl` with your tagging conventions.\n"
        "4. For each `terragrunt.hcl`, replace the placeholder module URL with\n"
        "   your AWS module repo path and fill in the `inputs = {}` block.\n"
        "5. Run `terragrunt run-all plan` to confirm the dependency graph.\n"
        "6. Run `terragrunt run-all apply` per environment, in the order shown\n"
        "   in `MIGRATION_GUIDE.md`.\n"
        "7. Run the data-migration helper scripts under `migration_helpers/`\n"
        "   to move bucket contents, secrets, container images, and database\n"
        "   data from GCP to AWS.\n\n"
        "## What's NOT done yet (Design phase)\n\n"
        "Per-resource AWS HCL inside the modules is **not** auto-generated by\n"
        "this skeleton — that's the Design phase, deferred until after this\n"
        "demo. The skeleton gives you the *structure* (file layout, dependency\n"
        "wiring, root config) so your engineers fill in the resource-specific\n"
        "AWS HCL with full context from the source GCP inputs (commented inline\n"
        "in each stack's `terragrunt.hcl`).\n"
    )


def _collect_source_inputs(resources: List[DiscoveredResource]) -> Dict[str, object]:
    """Merge inputs from all resources at this stack location."""
    out: Dict[str, object] = {}
    for r in resources:
        if isinstance(r.arguments, dict):
            for k, v in r.arguments.items():
                out.setdefault(k, v)
    return out


def _stringify(v: object) -> str:
    """Best-effort one-line representation of a value for inline comments."""
    if isinstance(v, str):
        return f'"{v}"'
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    return repr(v)


def _write_text(path: str, content: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(content)
