"""Emit a scaffolded AWS pure-Terraform repo (no Terragrunt wrapper).

Companion to terragrunt_emitter — same translation pipeline, different
wrapping. Used when the source repo is vanilla Terraform (no
terragrunt.hcl files). Produces:

    target/
    ├── modules/                    # AWS module bodies (re-used)
    │   ├── s3-bucket/
    │   ├── vpc/
    │   ├── ec2-instance/
    │   └── ...
    ├── environments/
    │   ├── dev/
    │   │   ├── main.tf            # one `module {}` block per source resource
    │   │   ├── variables.tf
    │   │   ├── outputs.tf
    │   │   ├── providers.tf       # AWS provider with default_tags
    │   │   ├── backend.tf         # S3 backend skeleton
    │   │   └── versions.tf
    │   ├── staging/
    │   └── prod/
    └── README.md

Detection of "envs" in the source: any directory matching
`<repo>/environments/<name>/main.tf` is treated as a root module.
If no such pattern is found, falls back to a single `target/main.tf`
at root containing every translated resource.

Per-resource translation reuses the same 19 translators that drive
terragrunt mode. Translators expect terragrunt-shaped inputs (lists/
maps of resource configs); we adapt vanilla-Terraform resource args
into that shape via _TF_NORMALIZE before calling translate_resource.

Post-emission `terraform fmt -recursive` runs against the target tree
(best-effort; skipped if terraform CLI not on PATH).
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
from typing import Dict, List, Optional, Set

from migrator.results import ConfidenceFinding, DiscoveredResource
from migrator.translate import all_aws_module_specs, translate_resource


logger = logging.getLogger(__name__)


_DEFAULT_AWS_REGION = "us-east-1"


# How to convert a vanilla-Terraform resource's flat args into the
# terragrunt-shaped {key: list/dict} that translators expect.
#
# Shape codes:
#   "list"        : produces {key: [args_with_name]}
#   "dict"        : produces {key: {resource_name: args}}
#   "dict_single" : produces {key: args}  (translator handles a single record)
_TF_NORMALIZE: Dict[str, tuple] = {
    "google_storage_bucket":                  ("buckets", "list"),
    "google_compute_instance":                ("instances", "list"),
    "google_compute_address":                 ("internal_addresses", "dict"),
    "google_compute_global_address":          ("global_addresses", "dict"),
    "google_redis_instance":                  ("instances", "list"),
    "google_compute_router_nat":              ("nat_configs", "dict"),
    "google_sql_database_instance":           ("sql_config", "dict_single"),
    "google_pubsub_topic":                    ("topics", "dict"),
    "google_pubsub_subscription":             ("subscriptions", "dict"),
    "google_secret_manager_secret":           ("secrets", "list"),
    "google_artifact_registry_repository":    ("repositories", "dict"),
    "google_certificate_manager_certificate": ("certificates", "list"),
    "google_dns_managed_zone":                ("managed_zones", "dict"),
    "google_compute_network":                 ("vpcs", "dict"),
    "google_compute_subnetwork":              ("subnets", "list"),
    "google_compute_firewall":                ("firewall_rules", "dict"),
    "google_compute_security_policy":         ("policies", "list"),
    "google_logging_project_sink":            ("sinks", "dict"),
    "google_cloud_scheduler_job":             ("jobs", "list"),
    # FCR v3 Week 2 (2026-05-11)
    # EKS translator accepts either gke_cluster_name (single cluster) OR
    # gke_config (multi-cluster). For vanilla-TF resources, we wrap as
    # single-cluster (gke_cluster_name from resource.name).
    "google_container_cluster":               ("gke_cluster_name", "dict_single"),
    "google_container_node_pool":             ("nodepool_config", "list"),
}


_IDENTIFIER_RE = re.compile(r"[^A-Za-z0-9_]+")


# Known source-side refs → AWS env-root equivalents. Applied as plain
# substring replacement, longest-key-first. python-hcl2 mangles
# `${var.X}` → `${var_X}` inside dict keys, so we match both forms.
# Same applies to `${local.X}` → `${local_X}` mangling.
_SOURCE_REF_SUBSTITUTIONS = [
    # var.X
    ("${var.environment}", "${local.environment}"),
    ("${var_environment}", "${local.environment}"),
    ("${var.region}",      "${local.region}"),
    ("${var_region}",      "${local.region}"),
    ("${var.labels}",      "${local.common_tags}"),
    ("${var_labels}",      "${local.common_tags}"),
    ("var.environment",    "local.environment"),
    ("var.region",         "local.region"),
    ("var.labels",         "local.common_tags"),
    # local.env (the customer's terragrunt source pattern). python-hcl2
    # mangles `${local.env}` → `${local_env}` in dict-key positions.
    ("${local.env}",       "${local.environment}"),
    ("${local_env}",       "${local.environment}"),
    # Other common GCP→AWS local rename patterns from customer's source:
    ("${local._project.locals.project_id}", "${local.environment}"),
    ("${local._env_configs.locals.env}",    "${local.environment}"),
]


# Locals defined at env root level (the env main.tf locals block).
_ENV_KNOWN_LOCALS = (
    "local.environment",
    "local.region",
    "local.common_tags",
)


def _is_known_local(ref: str) -> bool:
    """True if `ref` (e.g. 'local.environment.name') is a known env-root
    local or a sub-attribute of one."""
    for known in _ENV_KNOWN_LOCALS:
        if ref == known or ref.startswith(known + "."):
            return True
    return False


# Two-form regexes per ref type — interpolation vs. bare. The
# interpolation form preserves the `${...}` wrapper in the replacement
# (otherwise we'd corrupt the surrounding string literal). The bare
# form replaces with a quoted string literal.

# ${var.NAME}  or  ${var_NAME}  (python-hcl2 dict-key mangling)
_VAR_INTERP_RE = re.compile(r"\$\{var[._]([A-Za-z0-9_]+)\}")
# bare var.NAME (with negative lookbehind to avoid matching inside ${...})
_VAR_BARE_RE = re.compile(r"(?<![\w.${])var\.([A-Za-z0-9_]+)")

_LOCAL_INTERP_RE = re.compile(r"\$\{(local\.[A-Za-z0-9_.\-]+)\}")
_LOCAL_BARE_RE = re.compile(r"(?<![\w.${])(local\.[A-Za-z0-9_.\-]+)")
# python-hcl2 dict-key mangling: ${local.X} → ${local_X}, ${local.X.Y} → ${local_X_Y}.
# Captured group is everything after `local_`. Allow the captured name
# to start with `_` because the customer's source has locals like
# `_project` and `_env_configs` (leading-underscore convention) that
# mangle to `${local__project_locals_env}` (double underscore at start).
_LOCAL_MANGLED_INTERP_RE = re.compile(r"\$\{local_([A-Za-z_][A-Za-z0-9_]*)\}")

_EACH_INTERP_RE = re.compile(r"\$\{each\.(value|key)((?:\.[A-Za-z0-9_.\-]+)?)\}")
_EACH_BARE_RE = re.compile(r"(?<![\w.${])each\.(value|key)((?:\.[A-Za-z0-9_.\-]+)?)")
# python-hcl2 dict-key mangling: ${each.key} → ${each_key}, ${each.value.x} → ${each_value_x}
_EACH_MANGLED_INTERP_RE = re.compile(r"\$\{each_(key|value)((?:_[A-Za-z0-9_]+)?)\}")

# ${dependency.X.outputs.Y[...]...} — Terragrunt-only references to
# other-stack outputs. No analog in vanilla Terraform; in target mode
# we'd need module output references, which we don't have wired.
# Replace with TODO placeholders.
# python-hcl2 mangles these in dict-key positions into forms like
# ${dependency_vpc_id_outputs_vpc["..."]_name}. Match both shapes.
_DEPENDENCY_INTERP_RE = re.compile(r"\$\{dependency\.[^}]+\}")
_DEPENDENCY_MANGLED_INTERP_RE = re.compile(r"\$\{dependency_[^}]+\}")
_DEPENDENCY_BARE_RE = re.compile(r"(?<![\w.${])dependency\.[A-Za-z0-9_.\[\]\"\-]+")


def _sanitize_translation(text: str) -> str:
    """Sanitize translator output for terraform-mode emission.

    Translators emit refs like `${var.environment}` or `each.value.x`
    that scope inside the SOURCE GCP module body. Once the translation
    is embedded in an env-root `module {}` call, those refs reference
    nothing — `terraform validate` rightly fails.

    Strategy:
      1. Substitute known refs to env-root equivalents (var.environment
         → local.environment, etc.)
      2. For any remaining refs:
         - Interpolation form ${X}  → ${"TODO-..."}  (keep wrapper)
         - Bare form X              → "TODO-..."     (quoted literal)
    """
    out = text

    # Step 1: known substitutions (literal string replace, longest-first).
    for src, dst in _SOURCE_REF_SUBSTITUTIONS:
        out = out.replace(src, dst)

    # Step 2a: var.X — never resolves at env root unless it's a var we
    # add to env variables.tf. We don't, so always replace with TODO.
    out = _VAR_INTERP_RE.sub(lambda m: f'${{"TODO-var-{m.group(1)}"}}', out)
    out = _VAR_BARE_RE.sub(lambda m: f'"TODO-var-{m.group(1)}"', out)

    # Step 2b: local.X — preserve known locals; TODO unknown ones.
    def _local_interp_sub(m):
        ref = m.group(1)
        if _is_known_local(ref):
            return m.group(0)
        slug = ref.replace(".", "-")
        return f'${{"TODO-{slug}"}}'

    def _local_bare_sub(m):
        ref = m.group(1)
        if _is_known_local(ref):
            return m.group(0)
        slug = ref.replace(".", "-")
        return f'"TODO-{slug}"'

    out = _LOCAL_INTERP_RE.sub(_local_interp_sub, out)
    out = _LOCAL_BARE_RE.sub(_local_bare_sub, out)

    # Step 2b': python-hcl2's mangled form: ${local_X} (no dot).
    # We can't recover the original attribute-path structure from a
    # mangled identifier (`local_primary_region_suffix` could have been
    # `local.primary_region_suffix` OR `local.primary.region.suffix`),
    # so always TODO-replace.
    def _local_mangled_sub(m):
        slug = m.group(1).replace("_", "-")
        return f'${{"TODO-local-{slug}"}}'
    out = _LOCAL_MANGLED_INTERP_RE.sub(_local_mangled_sub, out)

    # Step 2c: each.X — never resolves at env root.
    def _each_interp_sub(m):
        kind = m.group(1)
        suffix = (m.group(2) or "").lstrip(".")
        slug = f"each-{kind}" + (f"-{suffix.replace('.', '-')}" if suffix else "")
        return f'${{"TODO-{slug}"}}'

    def _each_bare_sub(m):
        kind = m.group(1)
        suffix = (m.group(2) or "").lstrip(".")
        slug = f"each-{kind}" + (f"-{suffix.replace('.', '-')}" if suffix else "")
        return f'"TODO-{slug}"'

    out = _EACH_INTERP_RE.sub(_each_interp_sub, out)
    out = _EACH_BARE_RE.sub(_each_bare_sub, out)

    # Catch python-hcl2's mangled form: ${each_key}, ${each_value_X}.
    def _each_mangled_sub(m):
        kind = m.group(1)
        suffix = (m.group(2) or "").lstrip("_")
        slug = f"each-{kind}" + (f"-{suffix.replace('_', '-')}" if suffix else "")
        return f'${{"TODO-{slug}"}}'
    out = _EACH_MANGLED_INTERP_RE.sub(_each_mangled_sub, out)

    # Step 2d: dependency.X — Terragrunt-only references to other stacks'
    # outputs. No analog in vanilla Terraform target mode; replace with
    # TODO placeholders so terraform validate doesn't error. Operator
    # will wire to module outputs during the manual review pass.
    out = _DEPENDENCY_INTERP_RE.sub(lambda m: '${"TODO-dependency-ref"}', out)
    out = _DEPENDENCY_MANGLED_INTERP_RE.sub(lambda m: '${"TODO-dependency-ref"}', out)
    out = _DEPENDENCY_BARE_RE.sub(lambda m: '"TODO-dependency-ref"', out)

    return out


def _safe_identifier(name: str) -> str:
    """Sanitize a string for use as an HCL block identifier."""
    s = _IDENTIFIER_RE.sub("_", str(name)).strip("_")
    if not s:
        s = "resource"
    if s[0].isdigit():
        s = f"r_{s}"
    return s


def _normalize_terraform_resource_args(resource: DiscoveredResource) -> Dict:
    """Adapt a vanilla-Terraform resource's args into a translator-compatible
    dict shape. No-op for unknown tf_types (caller falls back to scaffold).

    Skipped when args are ALREADY in terragrunt-shape — e.g. when the source
    is GCP Terragrunt and the resource came from `inputs = { gcs_config = [
    {...}, {...}]}`, the inner list is already what translators expect.
    Detection: if the args dict already contains the rule's target `key`,
    we assume it's pre-normalized and pass through unchanged."""
    rule = _TF_NORMALIZE.get(resource.tf_type)
    args = dict(resource.arguments or {})
    if not rule:
        return args
    key, shape = rule
    # Already in shape? Don't double-wrap. This is the GCP Terragrunt
    # source → AWS Terraform target path: synthetic resources from
    # terragrunt.hcl inputs already have keys like `buckets`, `gcs_config`,
    # `vm_configs`, etc. that translators consume directly.
    if key in args:
        return args
    if shape == "list":
        item = dict(args)
        item.setdefault("name", resource.name)
        return {key: [item]}
    if shape == "dict":
        return {key: {resource.name: dict(args)}}
    if shape == "dict_single":
        return {key: dict(args)}
    return args


def _translate_terraform_resource(
    resource: DiscoveredResource,
    source_iac: str = "terraform",
    compliance_profile: str = "none",
):
    """Translate a resource via the shared translator layer.

    For vanilla Terraform sources, args are normalized (wrapped in the
    expected terragrunt-shape). For Terragrunt sources, args are passed
    through verbatim — they're already terragrunt-shaped from being
    parsed out of `inputs = { ... }` blocks.
    """
    if source_iac == "terragrunt":
        # Args already in terragrunt-shape — translators consume them directly.
        return translate_resource(resource, compliance_profile=compliance_profile)
    normalized = _normalize_terraform_resource_args(resource)
    adapted = DiscoveredResource(
        tf_type=resource.tf_type,
        name=resource.name,
        module_path=resource.module_path,
        file_path=resource.file_path,
        arguments=normalized,
    )
    return translate_resource(adapted, compliance_profile=compliance_profile)


def _format_with_terraform(target_dir: str) -> bool:
    """Best-effort: `terraform fmt -recursive`. Never raises."""
    if shutil.which("terraform") is None:
        return False
    try:
        proc = subprocess.run(
            ["terraform", "fmt", "-recursive", target_dir],
            capture_output=True, text=True, timeout=60,
        )
        return proc.returncode == 0
    except (subprocess.TimeoutExpired, OSError):
        return False


def _detect_source_environments(repo_path: str) -> List[str]:
    """Return source env names — directories under `<repo>/environments/`
    that contain a main.tf. Empty list if no env structure detected.
    """
    env_dir = os.path.join(repo_path, "environments")
    if not os.path.isdir(env_dir):
        # Try alternative common pattern: envs/, env/
        for alt in ("envs", "env"):
            cand = os.path.join(repo_path, alt)
            if os.path.isdir(cand):
                env_dir = cand
                break
        else:
            return []
    envs: List[str] = []
    for entry in sorted(os.listdir(env_dir)):
        sub = os.path.join(env_dir, entry)
        if os.path.isdir(sub) and os.path.isfile(os.path.join(sub, "main.tf")):
            envs.append(entry)
    return envs


# Cap on how many resources we'll put into a single target root.
# When a source group's resource count exceeds this, the validator
# slows to a crawl (`terraform init` is O(n) on module copies).
# Splitting large groups keeps each root validating in seconds, and
# the parallel validator pays off because each chunk is independent.
_MAX_RESOURCES_PER_ROOT = 100


def _group_resources_for_terragrunt_source(
    resources: List[DiscoveredResource],
) -> Dict[str, List[DiscoveredResource]]:
    """Group resources into target-root buckets based on their source module_path.

    Strategy: take the first 2 path segments of each resource's source
    `module_path` and use that as the group key. For the customer's
    simple-gcp fixture this naturally creates groups like:
        common-common-mgmt
        common-common-network
        environments-dev
        environments-staging
        environments-prod
        ...

    Then split any group with > _MAX_RESOURCES_PER_ROOT resources into
    consecutive chunks (`<group>-part1`, `<group>-part2`, ...) so each
    target root stays validation-tractable.

    Returns a dict mapping group_name → resources, with stable ordering.
    """
    raw: Dict[str, List[DiscoveredResource]] = {}
    for r in resources:
        parts = (r.module_path or "default").split("/")
        # Use first 2 segments for grouping; pad with "_root" if shorter.
        if len(parts) >= 2:
            group = f"{parts[0]}-{parts[1]}"
        else:
            group = parts[0] if parts[0] else "default"
        # Sanitize for use as a filesystem directory + HCL identifier
        group = _IDENTIFIER_RE.sub("_", group).strip("_") or "default"
        raw.setdefault(group, []).append(r)

    # Now split oversized groups into chunks.
    out: Dict[str, List[DiscoveredResource]] = {}
    for group, group_resources in sorted(raw.items()):
        if len(group_resources) <= _MAX_RESOURCES_PER_ROOT:
            out[group] = group_resources
            continue
        # Split into chunks of _MAX_RESOURCES_PER_ROOT.
        for i in range(0, len(group_resources), _MAX_RESOURCES_PER_ROOT):
            chunk = group_resources[i:i + _MAX_RESOURCES_PER_ROOT]
            chunk_idx = (i // _MAX_RESOURCES_PER_ROOT) + 1
            out[f"{group}_part{chunk_idx}"] = chunk
    return out


def emit_terraform_skeleton(
    *,
    output_dir: str,
    repo_path: str,
    target_cloud: str,
    resources: List[DiscoveredResource],
    confidence: List[ConfidenceFinding],
    aws_region: Optional[str] = None,
    source_iac: str = "terraform",
    compliance_profile: str = "none",
) -> List[str]:
    """Write the AWS pure-Terraform skeleton under <output_dir>/target/.

    Args:
        source_iac: "terraform" or "terragrunt". When "terragrunt",
            translator args are passed through verbatim (already in shape).
            Also affects env-detection fallback: Terragrunt repos rarely
            have `<repo>/environments/<env>/main.tf`, so we synthesize a
            single "default" env.

    Returns the list of absolute paths written.
    """
    if target_cloud.lower() != "aws":
        return []

    target_root = os.path.join(output_dir, "target")
    os.makedirs(target_root, exist_ok=True)

    aws_region = aws_region or _DEFAULT_AWS_REGION
    confidence_by_addr = {c.resource_address: c for c in confidence}
    confidence_by_type = {c.tf_type: c for c in confidence}

    written: List[str] = []

    # ---- 1. AWS module bodies (shared across all envs) ----
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

    # ---- 2. Decide how to subdivide the output into roots ----
    #
    # Two strategies:
    #
    #   A. Source is vanilla Terraform with `<repo>/environments/<env>/main.tf`
    #      shape → emit one target root per detected env, EACH receiving
    #      the full resource list (env-tier sizing differentiates them).
    #
    #   B. Source is Terragrunt (or Terraform without env layout) → group
    #      resources by their first 2 source-path segments and emit one
    #      target root PER GROUP, each receiving ONLY that group's
    #      resources. This is the "subdivide for validation tractability"
    #      change — previously the fallback was a single root with all
    #      941 resources, which made `terraform init` take minutes.
    if source_iac == "terraform":
        envs = _detect_source_environments(repo_path)
    else:
        envs = []

    if envs:
        # Strategy A: per-env layout (Terraform source with environments/<env>/).
        root_groups: Dict[str, List[DiscoveredResource]] = {
            env: resources for env in envs
        }
    else:
        # Strategy B: group by source path prefix. Always produces at
        # least one group; for tiny single-leaf inputs that's one root.
        root_groups = _group_resources_for_terragrunt_source(resources)
        if not root_groups:
            root_groups = {"default": resources}

    env_layout_root = os.path.join(target_root, "environments")
    os.makedirs(env_layout_root, exist_ok=True)

    # ---- 3. Per-root emission ----
    for root_name, root_resources in root_groups.items():
        env_dir = os.path.join(env_layout_root, root_name)
        os.makedirs(env_dir, exist_ok=True)

        # main.tf — one module {} block per source resource in this group.
        # Path from env_dir up to target/modules/ is always 2 levels
        # (env_dir = target/environments/<root_name>/).
        modules_relpath = "../../modules"

        main_tf = _render_env_main_tf(
            env_name=root_name,
            aws_region=aws_region,
            resources=root_resources,
            confidence_by_addr=confidence_by_addr,
            confidence_by_type=confidence_by_type,
            modules_relpath=modules_relpath,
            available_module_services=emitted_module_specs,
            source_iac=source_iac,
            compliance_profile=compliance_profile,
        )
        main_path = os.path.join(env_dir, "main.tf")
        _write_text(main_path, main_tf)
        written.append(main_path)

        for fname, content in (
            ("variables.tf", _render_env_variables_tf(aws_region)),
            ("outputs.tf",   _render_env_outputs_tf()),
            ("providers.tf", _render_env_providers_tf()),
            ("backend.tf",   _render_env_backend_tf(root_name)),
            ("versions.tf",  _render_env_versions_tf()),
        ):
            full = os.path.join(env_dir, fname)
            _write_text(full, content)
            written.append(full)

    # For the README rendering below, the list of "env names" is the
    # set of root group names we emitted.
    envs = list(root_groups.keys())

    # ---- 4. README ----
    readme_path = os.path.join(target_root, "README.md")
    _write_text(readme_path, _render_target_readme(
        repo_path=repo_path,
        aws_region=aws_region,
        envs=envs,
        translated_services=sorted(emitted_module_specs),
    ))
    written.append(readme_path)

    # ---- 5. Best-effort canonical formatting ----
    if _format_with_terraform(target_root):
        logger.info("emitter_terraform_format_applied", extra={"target": target_root})

    return written


# -----------------------------------------------------------------
# Per-env main.tf rendering
# -----------------------------------------------------------------

def _render_env_main_tf(
    *,
    env_name: str,
    aws_region: str,
    resources: List[DiscoveredResource],
    confidence_by_addr: Dict[str, ConfidenceFinding],
    confidence_by_type: Dict[str, ConfidenceFinding],
    modules_relpath: str,
    available_module_services: Set[str],
    source_iac: str = "terraform",
    compliance_profile: str = "none",
) -> str:
    """One module {} block per source resource. Order: stable by module_path then name."""
    from migrator.translate.compliance_profiles import list_services_hardened_by
    _hardened_services = list_services_hardened_by(compliance_profile)

    lines: List[str] = []
    lines.append(f"# AWS Terraform root for env={env_name}")
    lines.append("# Synthesized by Cloud Lifecycle Intelligence — Migrator engine.")
    lines.append("# Each module {} block below corresponds to one source GCP resource.")
    lines.append("# Review per-resource source comments; replace TODO inputs before plan.")
    # Compliance profile banner — surfaces what defaults the operator picked.
    if compliance_profile and compliance_profile != "none":
        lines.append(f"#")
        lines.append(f"# Compliance profile: {compliance_profile.upper()}")
        lines.append(
            f"# Hardened defaults applied to: {', '.join(_hardened_services) or '(none yet — translators not wired for this profile)'}"
        )
        lines.append(
            f"# (e.g. block_public_access, KMS encryption, deletion_protection)"
        )
    else:
        lines.append(f"# Compliance profile: none (operator hardens each resource manually)")
    lines.append("")
    lines.append("locals {")
    lines.append(f'  environment = "{env_name}"')
    lines.append(f'  region      = "{aws_region}"')
    lines.append("")
    lines.append("  common_tags = {")
    lines.append(f'    environment = "{env_name}"')
    lines.append('    managed-by  = "terraform"')
    lines.append('    cost-center = "platform"')
    lines.append('    owner       = "platform-team"')
    lines.append("  }")
    lines.append("}")
    lines.append("")

    # Track block-name uniqueness within this file.
    used_names: Set[str] = set()

    for r in resources:
        conf = confidence_by_addr.get(r.address) or confidence_by_type.get(r.tf_type)
        translation = _translate_terraform_resource(
            r, source_iac=source_iac, compliance_profile=compliance_profile,
        )

        # Detect translator-exception stubs: the translate_resource()
        # wrapper returns a Translation with notes starting with
        # "translate-error:" when the per-type translator threw. The
        # aws_inputs_hcl in that case is empty/marker-only, so emitting
        # it as a live module call breaks `terraform validate` on
        # required-input checks. Downgrade to scaffold-only.
        translation_errored = (
            translation is not None
            and translation.notes
            and any(n.startswith("translate-error:") for n in translation.notes)
        )
        has_translation = (
            translation is not None
            and translation.service_name in available_module_services
            and not translation_errored
        )

        # De-duplicate across (module_path, name) collisions.
        base_name = _safe_identifier(f"{r.tf_type.replace('google_', '')}_{r.name}")
        block_name = base_name
        counter = 1
        while block_name in used_names:
            counter += 1
            block_name = f"{base_name}_{counter}"
        used_names.add(block_name)

        # Per-resource comment header.
        lines.append("# -----------------------------------------------------------------")
        lines.append(f"# Source: {r.tf_type}.{r.name}  ({r.module_path})")
        if conf:
            lines.append(f"# Confidence: {conf.band} ({conf.score_pct}%) — "
                         f"AWS equivalent: {conf.aws_equivalent or 'MANUAL_REVIEW'}")
            if conf.reason:
                lines.append(f"# Reason: {conf.reason}")
        if has_translation:
            lines.append(f"# Status: TRANSLATED → module ./modules/{translation.service_name}/")
            if translation.notes:
                for note in translation.notes:
                    lines.append(f"# Note: {note}")
        elif conf and conf.aws_equivalent == "MANUAL_REVIEW":
            lines.append("# Status: MANUAL_REVIEW — no direct AWS equivalent; "
                         "edit this block to point at chosen AWS service.")
        else:
            lines.append("# Status: SCAFFOLD-ONLY — translator pending; "
                         "block is commented out below.")
        lines.append("# -----------------------------------------------------------------")

        # Source GCP arguments — abridged inline reference comments.
        # CRITICAL: strip newlines from string values. Source repos
        # commonly have multi-line strings (SQL queries, YAML blobs,
        # certificate bodies) that would otherwise break the `#`
        # comment over multiple lines, leaving line 2+ to be parsed
        # as HCL — which usually isn't valid HCL → Tier 0 parse fails.
        if isinstance(r.arguments, dict):
            for k in sorted(r.arguments.keys()):
                if k.startswith("_"):
                    continue
                v = r.arguments[k]
                v_str = _stringify(v)
                # Collapse newlines (and any surrounding whitespace) so
                # the rendered comment stays on ONE line.
                v_str = " ".join(v_str.split())
                if len(v_str) > 100:
                    v_str = v_str[:97] + "..."
                lines.append(f"#   src.{k} = {v_str}")
        lines.append("")

        if has_translation:
            lines.append(f'module "{block_name}" {{')
            lines.append(f'  source = "{modules_relpath}/{translation.service_name}"')
            lines.append("")
            # Sanitize source-side var/local/each refs that don't resolve
            # in env-root scope. Without this, translator output like
            # `name = "${var.environment}-foo"` survives into the env's
            # main.tf and `terraform validate` fails on undeclared vars.
            inputs_body = _sanitize_translation(translation.aws_inputs_hcl).rstrip()
            if inputs_body:
                lines.append(inputs_body)
            lines.append("}")
            lines.append("")
            # Note: env-wide common_tags propagate via provider default_tags
            # (see providers.tf) — no per-module tags arg needed (and adding
            # one would conflict with translator-emitted `tags = ...`).
        elif conf and conf.aws_equivalent == "MANUAL_REVIEW":
            lines.append(f'# module "{block_name}" {{')
            lines.append(f'#   source = "{modules_relpath}/<TBD-aws-service>"')
            lines.append("#   # ⚠️ Operator: choose AWS service then translate args above.")
            lines.append("# }")
            lines.append("")
        else:
            aws_svc_slug = (
                conf.aws_equivalent.removeprefix("aws_").replace("_", "-")
                if conf and conf.aws_equivalent and conf.aws_equivalent != "MANUAL_REVIEW"
                else "TBD"
            )
            lines.append(f'# module "{block_name}" {{')
            lines.append(f'#   source = "{modules_relpath}/{aws_svc_slug}"')
            lines.append("#   # TODO: register translator at migrator/translate/<service>.py")
            lines.append("# }")
            lines.append("")

    return "\n".join(lines) + "\n"


def _render_env_variables_tf(aws_region: str) -> str:
    return (
        "# Inputs to this AWS Terraform root.\n\n"
        'variable "aws_account_id" {\n'
        "  type        = string\n"
        '  description = "AWS account ID this env deploys into."\n'
        '  default     = "REPLACE-WITH-AWS-ACCOUNT-ID"\n'
        "}\n\n"
        'variable "region" {\n'
        "  type        = string\n"
        '  description = "AWS region for primary resources."\n'
        f'  default     = "{aws_region}"\n'
        "}\n"
    )


def _render_env_outputs_tf() -> str:
    return (
        "# Re-export module outputs for cross-stack consumption.\n"
        "# Add per-module re-exports as needed (Migrator-emitted modules\n"
        "# already document their own outputs in modules/<service>/outputs.tf).\n"
    )


def _render_env_providers_tf() -> str:
    return (
        'provider "aws" {\n'
        "  region = local.region\n\n"
        "  default_tags {\n"
        "    tags = local.common_tags\n"
        "  }\n"
        "}\n"
    )


def _render_env_backend_tf(env_name: str) -> str:
    return (
        "# Remote state — S3 backend skeleton. Bucket/table must exist\n"
        "# before `terraform init`. Replace placeholders before deploy.\n\n"
        "terraform {\n"
        '  backend "s3" {\n'
        '    bucket         = "REPLACE-WITH-AWS-ACCOUNT-ID-tfstate"\n'
        f'    key            = "envs/{env_name}/terraform.tfstate"\n'
        '    region         = "us-east-1"\n'
        "    encrypt        = true\n"
        '    dynamodb_table = "tfstate-lock"\n'
        "  }\n"
        "}\n"
    )


def _render_env_versions_tf() -> str:
    return (
        "terraform {\n"
        '  required_version = ">= 1.5.0, < 2.0.0"\n'
        "  required_providers {\n"
        "    aws = {\n"
        '      source  = "hashicorp/aws"\n'
        '      version = "~> 5.20"\n'
        "    }\n"
        "  }\n"
        "}\n"
    )


def _render_target_readme(
    *,
    repo_path: str,
    aws_region: str,
    envs: List[str],
    translated_services: List[str],
) -> str:
    services_block = (
        "\n".join(f"- `{s}`" for s in translated_services)
        if translated_services else "_(none yet)_"
    )
    envs_block = "\n".join(f"- `environments/{e}/`" for e in envs)
    return (
        "# AWS pure-Terraform skeleton\n\n"
        "Generated by **Cloud Lifecycle Intelligence — Migrator engine** from\n"
        f"`{os.path.abspath(repo_path)}`.\n\n"
        f"- **Default AWS region:** `{aws_region}`\n"
        f"- **AWS module bodies emitted:** {len(translated_services)}\n"
        f"{services_block}\n\n"
        "## Environments\n\n"
        f"{envs_block}\n\n"
        "Each env directory is a Terraform root: cd into it and run\n"
        "`terraform init && terraform plan`.\n\n"
        "## Before deploy\n\n"
        "1. Update `backend.tf` in each env with your real S3 bucket + lock table.\n"
        "2. Replace `REPLACE-WITH-AWS-ACCOUNT-ID` placeholders.\n"
        "3. Each `module {}` block in `main.tf` has the source GCP arguments\n"
        "   listed as inline `# src.X = ...` comments — review before plan.\n"
        "4. TODO markers in inputs (e.g. `TODO-vpc-id`) need real AWS resource\n"
        "   IDs from your landing zone (or other module outputs).\n"
    )


# -----------------------------------------------------------------
# helpers
# -----------------------------------------------------------------

def _stringify(v: object) -> str:
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
