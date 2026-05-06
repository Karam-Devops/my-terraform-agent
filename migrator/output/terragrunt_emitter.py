"""Emit a scaffolded AWS Terragrunt repo mirroring the source structure.

Two emission modes per leaf stack:

  1. **Translated** (Design phase): the resource type has a registered
     translator in ``migrator.translate``. We emit:
       * a populated ``inputs = { ... }`` block with AWS-equivalent
         values mapped from the source GCP inputs
       * a leaf ``terragrunt.hcl`` whose ``terraform { source }`` points
         at a local AWS module under ``target/modules/<service>/``
         via the swap-friendly ``modules.hcl`` lookup pattern
     The corresponding AWS module body (main.tf + variables.tf +
     outputs.tf + versions.tf + README.md) is also emitted under
     ``target/modules/<service>/``.

  2. **Scaffold-only** (pre-Design): the resource type has no
     translator yet. We emit a stub leaf ``terragrunt.hcl`` with
     source GCP inputs as inline comments and an empty
     ``inputs = {}`` block — operator-fillable.

Post-emission steps:
  * **GCP→AWS local-reference substitution** — translated ``inputs = {...}``
    blocks may carry over verbatim references to source GCP locals
    like ``${local._project.locals.project_id}`` or
    ``${local._env_configs.locals.env}``. These resolve in the source
    repo via per-project ``project.hcl`` / ``env.hcl`` includes; in our
    AWS target we substitute them to AWS-equivalents
    (``local.environment``, ``local.region``, ``local.account_id``)
    defined at the leaf level via a synthesized locals block.
  * **terragrunt hcl format** — best-effort canonicalization so Tier 1
    of the validation report passes by construction. Skipped if the
    terragrunt CLI isn't on PATH.

The swap-friendly architecture lets the customer replace AWS module
sources later by editing one file (``target/_common/modules.hcl``).
See module-by-module README.md for the input/output contract.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
from typing import Dict, List, Optional, Set

from migrator.results import ConfidenceFinding, DiscoveredResource
from migrator.translate import all_aws_module_specs, translate_resource


logger = logging.getLogger(__name__)


# Default AWS region target — operator overrides via the Clarify form.
_DEFAULT_AWS_REGION = "us-east-1"


# GCP-style local references → AWS-equivalent local references.
# Applied as straight string substitution after the per-stack rendering.
# Order matters: longer keys first so we don't partially-match shorter ones.
_GCP_TO_AWS_LOCAL_REFS = [
    # _project.locals.* — customer's project.hcl pattern
    ("local._project.locals.project_id",            "local.environment"),
    ("local._project.locals.primary_region",        "local.region"),
    ("local._project.locals.primary_zone",          '"${local.region}a"'),
    ("local._project.locals.primary_region_suffix", "local.region"),
    ("local._project.locals.project_number",        "local.account_id"),
    # _env_configs / env.hcl pattern
    ("local._env_configs.locals.env",               "local.environment"),
    ("local._env_configs.locals.environment",       "local.environment"),
    # Bare local.env (less common)
    # NOTE: this is fragile — `local.env` could be a name collision in
    # other contexts. Apply last so other rules with longer prefixes
    # have already substituted out.
    ("local.env ",   "local.environment "),   # space-suffix to avoid prefix-match
    ("local.env}",   "local.environment}"),   # interpolation tail
    ("local.env)",   "local.environment)"),
    ("local.env,",   "local.environment,"),
    ("local.env\n",  "local.environment\n"),
    # python-hcl2 sometimes emits `${local.env}` as `${local_env}` —
    # underscored token. Catch both shapes.
    ("${local_env}",  "${local.environment}"),
    ("local_env",     "local.environment"),
]


def _substitute_gcp_local_refs(text: str) -> str:
    """Rewrite GCP-style local references to AWS-equivalents.

    Applied to each leaf's rendered HCL after the translator produces
    the inputs block. Pure string substitution — preserves everything
    else verbatim.
    """
    out = text
    for src, dst in _GCP_TO_AWS_LOCAL_REFS:
        out = out.replace(src, dst)
    return out


def _format_with_terragrunt(target_dir: str) -> bool:
    """Best-effort: run `terragrunt hcl format` to canonicalize the
    emitted tree. Returns True on success, False if terragrunt isn't
    on PATH or the command failed. Never raises.
    """
    if shutil.which("terragrunt") is None:
        return False
    try:
        proc = subprocess.run(
            ["terragrunt", "hcl", "format",
             "--working-dir", target_dir, "--no-color"],
            capture_output=True, text=True, timeout=60,
        )
        return proc.returncode == 0
    except (subprocess.TimeoutExpired, OSError):
        return False


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

    # ---- 2. _common/ shared locals + tags + modules-source-config ----
    common_dir = os.path.join(target_root, "_common")
    os.makedirs(common_dir, exist_ok=True)

    account_path = os.path.join(common_dir, "account.hcl")
    _write_text(account_path, _render_account_hcl())
    written.append(account_path)

    tags_path = os.path.join(common_dir, "tags.hcl")
    _write_text(tags_path, _render_tags_hcl())
    written.append(tags_path)

    # Swap-friendly modules config: customer can flip _modules_base from
    # local relative path to their AWS module repo with one edit.
    modules_path = os.path.join(common_dir, "modules.hcl")
    _write_text(modules_path, _render_modules_hcl())
    written.append(modules_path)

    # Per-environment env.hcl files (synthesized so leaves can include them).
    # Mirror every env.hcl from source at the same relative path in target,
    # plus emit a fallback env.hcl at the input root so leaves whose module
    # path doesn't have an env.hcl ancestor still resolve `find_in_parent_folders("env.hcl")`.
    env_paths = _emit_env_hcls(target_root, repo_path, resources)
    written.extend(env_paths)

    # ---- 3. AWS module bodies (one dir per registered translator service) ----
    modules_dir = os.path.join(target_root, "modules")
    os.makedirs(modules_dir, exist_ok=True)

    emitted_module_specs: Set[str] = set()
    for spec in all_aws_module_specs():
        svc_dir = os.path.join(modules_dir, spec.service_name)
        os.makedirs(svc_dir, exist_ok=True)

        for fname, content in (
            ("main.tf",      spec.main_tf),
            ("variables.tf", spec.variables_tf),
            ("outputs.tf",   spec.outputs_tf),
            ("versions.tf",  spec.versions_tf),
        ):
            full = os.path.join(svc_dir, fname)
            _write_text(full, content)
            written.append(full)

        if spec.readme_md:
            readme = os.path.join(svc_dir, "README.md")
            _write_text(readme, spec.readme_md)
            written.append(readme)

        emitted_module_specs.add(spec.service_name)

    # ---- 4. Per-stack leaf terragrunt.hcl ----
    # Group resources by source module_path. Each group is one leaf.
    stacks: Dict[str, List[DiscoveredResource]] = {}
    for r in resources:
        stacks.setdefault(r.module_path, []).append(r)

    for module_path, stack_resources in sorted(stacks.items()):
        if module_path in (".", ""):
            continue

        target_stack_dir = os.path.join(target_root, module_path)
        os.makedirs(target_stack_dir, exist_ok=True)

        target_stack_path = os.path.join(target_stack_dir, "terragrunt.hcl")
        rep = stack_resources[0]
        rep_conf = confidence_by_addr.get(rep.address)

        # Run the per-type translator. Returns None if no translator
        # registered for this type — caller falls back to scaffold-only.
        translation = translate_resource(rep)

        rendered = _render_stack_terragrunt(
            module_path=module_path,
            resources=stack_resources,
            confidence=rep_conf,
            translation=translation,
            available_module_services=emitted_module_specs,
        )
        _write_text(target_stack_path, rendered)
        written.append(target_stack_path)

    # ---- 5. Top-level README inside target/ ----
    readme_path = os.path.join(target_root, "README.md")
    _write_text(readme_path, _render_target_readme(
        repo_path=repo_path,
        aws_region=aws_region,
        stack_count=len(stacks),
        translated_services=sorted(emitted_module_specs),
    ))
    written.append(readme_path)

    # ---- 6. Best-effort canonical formatting via terragrunt CLI ----
    # If terragrunt is on PATH, run `terragrunt hcl format` to make
    # every file conform to canonical formatting. This makes Tier 1
    # of the validation report pass by construction (rather than
    # always failing because our Python templates don't match
    # terragrunt's exact whitespace rules).
    formatted = _format_with_terragrunt(target_root)
    if formatted:
        logger.info("emitter_terragrunt_format_applied", extra={"target": target_root})

    return written


# -----------------------------------------------------------------
# env.hcl emission
# -----------------------------------------------------------------

def _emit_env_hcls(
    target_root: str,
    repo_path: str,
    resources: List[DiscoveredResource],
) -> List[str]:
    """Mirror every env.hcl from source at the same relative path in target.

    Customer's GCP repo has env.hcl files at various depths
    (e.g. environments/dev/env.hcl). Their leaf terragrunt.hcl files
    do `read_terragrunt_config(find_in_parent_folders("env.hcl"))`, so
    the AWS skeleton needs equivalent env.hcl files at the same relative
    paths or terragrunt resolution fails.

    Strategy:
      1. Walk source `repo_path` for any file named `env.hcl`. For each,
         emit a synthesized AWS-flavored env.hcl at the same relative
         path under target/ — preserves the parent-folder lookup chain.
      2. Always emit a fallback env.hcl at target/ root so leaves whose
         path has no env.hcl ancestor still resolve.
    """
    written: List[str] = []
    seen_paths: Set[str] = set()
    abs_repo = os.path.abspath(repo_path)

    # 1. Mirror every source env.hcl
    if os.path.isdir(abs_repo):
        for root, _dirs, files in os.walk(abs_repo):
            if "env.hcl" not in files:
                continue
            # Skip migrator output trees if any were committed
            if "migrator_output" in root.split(os.sep):
                continue
            rel_dir = os.path.relpath(root, abs_repo).replace(os.sep, "/")
            if rel_dir == ".":
                rel_dir = ""

            target_env_path = os.path.join(target_root, rel_dir, "env.hcl") \
                if rel_dir else os.path.join(target_root, "env.hcl")
            if target_env_path in seen_paths:
                continue
            seen_paths.add(target_env_path)

            # Derive env name from rel_dir's last segment, falling back
            # to "default" for the input-root case.
            env_name = rel_dir.split("/")[-1] if rel_dir else "default"
            _write_text(target_env_path, _render_env_hcl(env_name))
            written.append(target_env_path)

    # 2. Always-on fallback env.hcl at target root.
    fallback = os.path.join(target_root, "env.hcl")
    if fallback not in seen_paths:
        _write_text(fallback, _render_env_hcl("default"))
        written.append(fallback)

    return written


def _render_env_hcl(env_name: str) -> str:
    """Synthesize a per-environment env.hcl for the AWS target tree."""
    is_prod = env_name in ("prod", "stage", "production", "stg")
    return (
        f"# Per-environment locals for env={env_name} (AWS target).\n"
        f"# Synthesized by Cloud Lifecycle Intelligence Migrator.\n"
        f"# Read by leaf terragrunt.hcl files via\n"
        f"# read_terragrunt_config(find_in_parent_folders(\"env.hcl\")).\n\n"
        "locals {\n"
        f'  environment    = "{env_name}"\n'
        f'  is_production  = {"true" if is_prod else "false"}\n'
        "}\n"
    )


# -----------------------------------------------------------------
# Static templates
# -----------------------------------------------------------------

def _render_root_terragrunt(aws_region: str) -> str:
    return (
        "# AWS Terragrunt root — synthesized by Cloud Lifecycle Intelligence Migrator.\n"
        "# Replace the placeholder values below with your real AWS landing-zone\n"
        "# configuration before running `terragrunt run-all init`.\n"
        "#\n"
        "# Locals are hardcoded here (rather than read from sibling config files)\n"
        "# so the root validates standalone with `terragrunt hcl validate`. The\n"
        "# files at target/_common/account.hcl + tags.hcl are documentation/\n"
        "# checklist; they're optionally consumed by leaves via\n"
        "# `find_in_parent_folders('_common/account.hcl')` if you want per-account\n"
        "# overrides without editing this root.\n\n"
        "locals {\n"
        '  account_id  = "REPLACE-WITH-AWS-ACCOUNT-ID"\n'
        '  org_id      = "REPLACE-WITH-AWS-ORG-ID"\n'
        f'  region      = "{aws_region}"\n\n'
        "  default_tags = {\n"
        '    managed-by  = "terraform"\n'
        '    cost-center = "platform"\n'
        '    owner       = "platform-team"\n'
        '    compliance  = "internal"\n'
        '    repo        = "REPLACE-WITH-AWS-REPO-NAME"\n'
        "  }\n"
        "}\n\n"
        "remote_state {\n"
        '  backend = "s3"\n'
        "  generate = {\n"
        '    path      = "backend.tf"\n'
        '    if_exists = "overwrite_terragrunt"\n'
        "  }\n"
        "  config = {\n"
        '    bucket         = "${local.account_id}-tfstate"\n'
        '    key            = "${path_relative_to_include()}/terraform.tfstate"\n'
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
        "    tags = ${jsonencode(local.default_tags)}\n"
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
        "  aws_account_id = local.account_id\n"
        "  region         = local.region\n"
        "  tags           = local.default_tags\n"
        "}\n"
    )


def _render_account_hcl() -> str:
    return (
        "# AWS account-wide locals.\n"
        "# Replace placeholders with your real values before deploy.\n\n"
        "locals {\n"
        '  aws_account_id      = "REPLACE-WITH-AWS-ACCOUNT-ID"\n'
        '  org_id              = "REPLACE-WITH-AWS-ORG-ID"\n'
        '  identity_center_arn = "REPLACE-WITH-IDENTITY-CENTER-ARN"\n'
        "}\n"
    )


def _render_tags_hcl() -> str:
    return (
        "# Org-wide tags applied to every AWS resource that supports them.\n\n"
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


def _render_modules_hcl() -> str:
    """Swap-friendly module source config.

    Customer can flip from Migrator-emitted local modules to their own
    AWS module library by editing this file. Per-service overrides
    allow gradual adoption.
    """
    return (
        "# Module source configuration — controls which AWS modules every\n"
        "# leaf terragrunt.hcl pulls in. Migrator emits this file with\n"
        "# defaults pointing at the local target/modules/ tree.\n"
        "#\n"
        "# Three swap modes:\n"
        "#\n"
        "# 1. DEFAULT (today): use Migrator-emitted modules under target/modules/\n"
        '#    _modules_base = "../../../modules"\n'
        "#\n"
        "# 2. SWAP ENTIRELY to customer's own AWS module library:\n"
        '#    _modules_base = "git::https://github.com/<your-org>/aws-modules.git"\n'
        '#    _modules_ref  = "?ref=v1.0.0"\n'
        "#\n"
        "# 3. SELECTIVELY override per service via _module_overrides:\n"
        "#    _module_overrides = {\n"
        '#      "s3-bucket" = "git::https://github.com/<your-org>/aws-modules.git//s3-bucket?ref=v2.1.0"\n'
        "#    }\n"
        "\n"
        "locals {\n"
        '  _modules_base     = "../../../modules"\n'
        '  _modules_ref      = ""\n'
        "  _module_overrides = {}\n"
        "}\n"
    )


# -----------------------------------------------------------------
# Per-leaf rendering (translated and scaffold-only paths)
# -----------------------------------------------------------------

def _render_stack_terragrunt(
    *,
    module_path: str,
    resources,
    confidence,
    translation,                              # Optional[Translation]
    available_module_services: Set[str],
) -> str:
    """Render one stack's AWS terragrunt.hcl.

    Two paths:
      - translation present & service has an emitted module → translated
      - else → scaffold-only with TODO inputs
    """
    rep = resources[0]
    aws_eq = (
        confidence.aws_equivalent if confidence and confidence.aws_equivalent
        else "MANUAL_REVIEW"
    )
    band = confidence.band if confidence else "UNKNOWN"
    score = confidence.score_pct if confidence else 0
    reason = confidence.reason if confidence else "(no confidence rating available)"

    # Resolve the local module's relative path from this leaf back up
    # to target/modules/. Module path within target is module_path
    # (e.g. environments/dev/foo/gcs); we need to climb that many "../"
    # to reach the root, then descend into modules/<service>/.
    depth = module_path.count("/") + 1
    relative_to_modules = "/".join([".."] * depth) + "/modules"

    # Source URL: customer's commented inline.
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
    if translation and translation.notes:
        lines.append("# Translation notes:")
        for note in translation.notes:
            lines.append(f"#   - {note}")
    lines.append("")
    lines.append('include "root" {')
    lines.append("  path = find_in_parent_folders()")
    lines.append("}")
    lines.append("")

    # Decide translated vs scaffold-only path.
    has_translation = (
        translation is not None
        and translation.service_name in available_module_services
    )

    if has_translation:
        service_name = translation.service_name
        # Leaf locals block: read AWS-target's account.hcl + env.hcl so
        # leaves can reference `local.environment`, `local.account_id`,
        # `local.region` directly. Mirrors the customer's source pattern
        # of having per-leaf `read_terragrunt_config(find_in_parent_folders(...))`
        # for project + env config.
        lines.append("locals {")
        lines.append(
            '  _account = read_terragrunt_config(find_in_parent_folders("_common/account.hcl"))'
        )
        lines.append(
            '  _env     = read_terragrunt_config(find_in_parent_folders("env.hcl"))'
        )
        lines.append(
            '  _modules = read_terragrunt_config(find_in_parent_folders("_common/modules.hcl"))'
        )
        lines.append("")
        lines.append("  account_id  = local._account.locals.aws_account_id")
        lines.append("  environment = local._env.locals.environment")
        lines.append('  region      = "us-east-1"')
        lines.append("")
        lines.append(f'  _service_name  = "{service_name}"')
        lines.append("  _module_source = lookup(")
        lines.append("    local._modules.locals._module_overrides,")
        lines.append("    local._service_name,")
        lines.append('    "${local._modules.locals._modules_base}/${local._service_name}${local._modules.locals._modules_ref}"')
        lines.append("  )")
        lines.append("}")
        lines.append("")

        lines.append("terraform {")
        lines.append("  source = local._module_source")
        lines.append("}")
        lines.append("")

        # Source GCP inputs commented inline (translation reference).
        src_inputs = _collect_source_inputs(resources)
        if src_inputs:
            lines.append("# ---- Source GCP inputs (for translation reference) ----")
            for k, v in sorted(src_inputs.items()):
                if k.startswith("_"):
                    continue
                v_str = _stringify(v)
                if len(v_str) > 80:
                    v_str = v_str[:77] + "..."
                lines.append(f"# {k} = {v_str}")
            lines.append("")

        lines.append("inputs = {")
        # Apply GCP→AWS local-reference substitution to the
        # translator's rendered inputs HCL. Catches things like
        # `${local._project.locals.project_id}` carried over from
        # the source bucket name and rewrites to `${local.environment}`.
        substituted_inputs = _substitute_gcp_local_refs(translation.aws_inputs_hcl)
        lines.append(substituted_inputs.rstrip())
        lines.append("}")

    elif aws_eq == "MANUAL_REVIEW":
        # MANUAL_REVIEW types — emit deactivated source block.
        lines.append("# ⚠️ MANUAL REVIEW REQUIRED: no direct AWS equivalent.")
        lines.append("# This stack needs an architectural decision before it can be wired up.")
        lines.append("# Comment-out this terragrunt.hcl or remove it once the operator confirms")
        lines.append("# the appropriate AWS service (or that this stack is being retired).")
        lines.append("")
        lines.append("# terraform {")
        lines.append('#   source = "../../modules/<TBD-aws-service>"')
        lines.append("# }")
        lines.append("")
        lines.append("# inputs = {}")

    else:
        # Scaffold-only — type recognized but no translator yet.
        # Comment out the `terraform { source }` block so terragrunt
        # doesn't reference a module we never emitted. The structural
        # presence of the file is preserved (leaf count matches source);
        # operator wires in real values when a translator is registered.
        aws_module_slug = (
            aws_eq.removeprefix("aws_").replace("_", "-")
            if aws_eq != "MANUAL_REVIEW" else "TBD"
        )
        lines.append("# ⚠️ Scaffold-only: no translator registered for this resource type yet.")
        lines.append(f"# Add a translator at migrator/translate/<service>.py to populate")
        lines.append(f"# inputs and emit the AWS module body. Once registered, uncomment")
        lines.append(f"# the terraform { '{ source }' } block and inputs below.")
        lines.append("")

        src_inputs = _collect_source_inputs(resources)
        if src_inputs:
            lines.append("# ---- Source GCP inputs (for translation reference) ----")
            for k, v in sorted(src_inputs.items()):
                if k.startswith("_"):
                    continue
                v_str = _stringify(v)
                if len(v_str) > 80:
                    v_str = v_str[:77] + "..."
                lines.append(f"# {k} = {v_str}")
            lines.append("")

        # Source + inputs both commented — operator uncomments when translator lands.
        lines.append("# locals {")
        lines.append('#   _modules = read_terragrunt_config(find_in_parent_folders("_common/modules.hcl"))')
        lines.append(f'#   _service_name  = "{aws_module_slug}"')
        lines.append("#   _module_source = lookup(")
        lines.append("#     local._modules.locals._module_overrides,")
        lines.append("#     local._service_name,")
        lines.append('#     "${local._modules.locals._modules_base}/${local._service_name}${local._modules.locals._modules_ref}"')
        lines.append("#   )")
        lines.append("# }")
        lines.append("")
        lines.append("# terraform {")
        lines.append("#   source = local._module_source")
        lines.append("# }")
        lines.append("")
        lines.append("# inputs = {")
        lines.append("#   # TODO: translate from source GCP inputs above.")
        lines.append("# }")

    return "\n".join(lines) + "\n"


def _render_target_readme(
    *,
    repo_path: str,
    aws_region: str,
    stack_count: int,
    translated_services: List[str],
) -> str:
    services_block = (
        "\n".join(f"- `{s}`" for s in translated_services)
        if translated_services else "_(none yet)_"
    )
    return (
        "# AWS Terragrunt skeleton\n\n"
        "Generated by **Cloud Lifecycle Intelligence — Migrator engine** from\n"
        f"`{os.path.abspath(repo_path)}`.\n\n"
        f"- **Stacks emitted:** {stack_count}\n"
        f"- **Default AWS region:** `{aws_region}`\n"
        f"- **AWS module bodies emitted (Design-phase):** {len(translated_services)}\n"
        f"{services_block}\n\n"
        "## Module-source swap path\n\n"
        "The leaf `terragrunt.hcl` files don't hardcode the module path. They\n"
        "look it up against `_common/modules.hcl`, which has three modes:\n\n"
        "1. **DEFAULT (today)** — use Migrator-emitted local modules under\n"
        "   `target/modules/`. Ready to plan/apply against AWS.\n"
        "2. **SWAP ENTIRELY** — point `_modules_base` at your own AWS module\n"
        "   GitLab/GitHub repo. Every leaf switches in one edit.\n"
        "3. **SELECTIVE OVERRIDE** — add per-service entries to\n"
        "   `_module_overrides` for gradual adoption.\n\n"
        "## Before deploy\n\n"
        "1. Update `_common/account.hcl` with your AWS account ID, org ID, etc.\n"
        "2. Update `_common/tags.hcl` with your tagging conventions.\n"
        "3. Each leaf passes `inputs = {...}` translated from your source GCP\n"
        "   inputs — review the inline comments in each `terragrunt.hcl`\n"
        "   showing the original GCP values for reference.\n"
        "4. Wire VPC/subnet/SG IDs (TODO markers in the inputs blocks).\n"
        "5. Run `terragrunt run-all init` then `terragrunt run-all plan`\n"
        "   in dependency order. See `MIGRATION_GUIDE.md` (one dir up) for\n"
        "   the full deploy sequence.\n"
    )


# -----------------------------------------------------------------
# helpers
# -----------------------------------------------------------------

def _collect_source_inputs(resources):
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
