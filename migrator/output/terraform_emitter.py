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
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Set

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
    # FCR v3 Week 2 ALB (2026-05-12)
    "google_compute_global_forwarding_rule":  ("forwarding_rules", "list"),
    "google_compute_forwarding_rule":         ("forwarding_rules", "list"),
    "google_compute_backend_service":         ("lb_config", "dict_single"),
    # Kiro-review fix #8 (2026-05-12)
    "google_bigquery_dataset":                ("datasets", "list"),
    "google_bigquery_table":                  ("datasets", "list"),
}


_IDENTIFIER_RE = re.compile(r"[^A-Za-z0-9_]+")


# Source-side refs → AWS env-root equivalents.
# Loaded at runtime from customer_profiles/ YAML files (see
# migrator/translate/customer_profile_loader.py). The loader merges
# _default.yaml + an optional customer-named profile, sorts by source-ref
# length descending (longer keys check first to prevent prefix matches),
# and returns the substitution list.
#
# Onboarding a new customer: add a YAML profile under
# migrator/translate/customer_profiles/ — no code change needed.
#
# Legacy module-level constant retained for tests but not used at
# runtime — _sanitize_translation() now calls get_substitutions()
# directly with the active customer_profile.
_SOURCE_REF_SUBSTITUTIONS: list = []   # deprecated; profile-driven now


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


def _sanitize_translation(text: str, customer_profile: str = "default") -> str:
    """Sanitize translator output for terraform-mode emission.

    Translators emit refs like `${var.environment}` or `each.value.x`
    that scope inside the SOURCE GCP module body. Once the translation
    is embedded in an env-root `module {}` call, those refs reference
    nothing — `terraform validate` rightly fails.

    Strategy:
      1. Substitute known refs to env-root equivalents using the
         CUSTOMER PROFILE (loaded from customer_profiles/*.yaml).
         Profile-aware: customer-specific patterns (like CitiusTech's
         `${local._project.locals.project_id}`) substitute cleanly;
         everything else falls through to step 2.
      2. For any remaining refs:
         - Interpolation form ${X}  → ${"TODO-..."}  (keep wrapper)
         - Bare form X              → "TODO-..."     (quoted literal)
    """
    from migrator.translate.customer_profile_loader import get_substitutions
    out = text

    # Step 1: customer-profile substitutions (literal string replace,
    # longest-key-first per loader's ordering).
    for src, dst in get_substitutions(customer_profile):
        out = out.replace(src, dst)

    # Step 2a: var.X — never resolves at env root unless it's a var we
    # add to env variables.tf. We don't, so always replace with TODO.
    # The interpolation form `${var.X}` is replaced with the BARE
    # TODO marker (no surrounding `${...}`) so we don't create the
    # `${"TODO-var-X"}` antipattern Kiro flagged in v7 — that's a
    # string-literal-interpolation, syntactically valid but produces
    # broken map keys + meaningless reference values.
    out = _VAR_INTERP_RE.sub(lambda m: f'TODO-var-{m.group(1)}', out)
    out = _VAR_BARE_RE.sub(lambda m: f'"TODO-var-{m.group(1)}"', out)

    # Step 2b: local.X — preserve known locals; TODO unknown ones.
    # Same fix: emit bare `TODO-local-X` inside the surrounding string
    # rather than `${"TODO-local-X"}` which (a) is ugly and (b) breaks
    # when used as a map key (Kiro v7 fix #3+#5).
    def _local_interp_sub(m):
        ref = m.group(1)
        if _is_known_local(ref):
            return m.group(0)
        slug = ref.replace(".", "-")
        return f'TODO-{slug}'

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
    # so always TODO-replace. Bare form (no `${}` wrapper) for same
    # reason as above — keeps map keys clean.
    def _local_mangled_sub(m):
        slug = m.group(1).replace("_", "-")
        return f'TODO-local-{slug}'
    out = _LOCAL_MANGLED_INTERP_RE.sub(_local_mangled_sub, out)

    # Step 2c: each.X — never resolves at env root.
    # Bare-form replacement (no `${}` wrapper) for same Kiro v7 reason —
    # keeps map keys + string values clean.
    def _each_interp_sub(m):
        kind = m.group(1)
        suffix = (m.group(2) or "").lstrip(".")
        slug = f"each-{kind}" + (f"-{suffix.replace('.', '-')}" if suffix else "")
        return f'TODO-{slug}'

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
        return f'TODO-{slug}'
    out = _EACH_MANGLED_INTERP_RE.sub(_each_mangled_sub, out)

    # Step 2d: dependency.X — Terragrunt-only references to other stacks'
    # outputs. No analog in vanilla Terraform target mode; replace with
    # TODO placeholders so terraform validate doesn't error. Operator
    # will wire to module outputs during the manual review pass.
    # Interpolation form: bare replacement keeps strings + map keys clean.
    out = _DEPENDENCY_INTERP_RE.sub(lambda m: 'TODO-dependency-ref', out)
    out = _DEPENDENCY_MANGLED_INTERP_RE.sub(lambda m: 'TODO-dependency-ref', out)
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
        # Special case: EKS translator's Pattern A expects
        # `gke_cluster_name` to be a STRING (the cluster's name), with
        # vpc_config / nodepool_config / etc. at the SAME top level.
        # Generic dict_single wrapping puts the whole args under the
        # key as a DICT — wrong shape for eks. Caught by the plain-TF
        # audit (#2). Detect by key name + extract args["name"] as
        # the string value, preserve other fields at top level.
        if key == "gke_cluster_name":
            cluster_name = str(args.get("name") or resource.name)
            other_args = {k: v for k, v in args.items() if k != "name"}
            return {key: cluster_name, **other_args}
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
    customer_profile: str = "default",
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
            customer_profile=customer_profile,
        )
        # Post-render: promote operator-action TODOs in strict-format
        # fields (cidr, dns_name, ip_address, arn) into required
        # `var.X` references. The literal TODO strings would otherwise
        # fail terraform plan with "invalid CIDR block format" or
        # similar cryptic errors at AWS API call time. Now plan fails
        # earlier with "Variable X is required" + the variable's
        # description naming the unresolvable source local.
        # Kiro v9 Gap 1+2 final cleanup.
        main_tf, promoted_vars = _promote_strict_field_todos(main_tf)
        if promoted_vars:
            _write_text(os.path.join(env_dir, "main.tf"), main_tf)
        main_path = os.path.join(env_dir, "main.tf")
        _write_text(main_path, main_tf)
        written.append(main_path)

        # Cross-env variable declarations: scan the rendered main.tf for
        # `var.X` references that the cross_module_wiring layer substituted
        # in place of unresolvable in-env module refs, and add matching
        # `variable {}` blocks to variables.tf so terraform validate
        # stays green. Operators supply the actual values via tfvars or
        # workspace inputs.
        cross_env_vars_used = _scan_cross_env_vars(main_tf)
        # Merge in any vars promoted from strict-format TODOs (each
        # carries its own description naming the source local that
        # needs operator resolution).
        cross_env_vars_used = list(cross_env_vars_used) + sorted(
            promoted_vars.keys()
        )

        # Service modules emitted in this env — drives versions.tf
        # provider-required block (Kiro v7 fix #1: aurora uses random,
        # eks uses tls + random). Scan the rendered main.tf for
        # `source = "../../modules/<svc>/"` references to find which
        # service modules this env consumes. Cheap one-pass regex.
        services_in_env = sorted(set(re.findall(
            r'source\s*=\s*"\.\./\.\./modules/([^/"]+)/?"',
            main_tf,
        )))

        for fname, content in (
            ("variables.tf", _render_env_variables_tf(aws_region, cross_env_vars_used)),
            ("outputs.tf",   _render_env_outputs_tf()),
            ("providers.tf", _render_env_providers_tf()),
            ("backend.tf",   _render_env_backend_tf(root_name)),
            ("versions.tf",  _render_env_versions_tf(services_in_env)),
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

    # ---- 6. Prune unused module bodies ----
    # We emit ALL registered AWS module bodies under target/modules/
    # at step 1 for simplicity. Now that every env's main.tf is on
    # disk, we know exactly which modules are referenced. Delete the
    # unreferenced module directories so the operator's output tree
    # doesn't carry dead code (e.g., rds-postgres when everything
    # routed to aurora-postgres under HIPAA).
    used_services = _scan_referenced_modules(env_layout_root)
    if used_services:
        for spec_name in list(emitted_module_specs):
            if spec_name in used_services:
                continue
            unused_dir = os.path.join(modules_dir, spec_name)
            if os.path.isdir(unused_dir):
                shutil.rmtree(unused_dir, ignore_errors=True)
                # Also remove from the `written` list so the UI doesn't
                # advertise files that no longer exist.
                written = [p for p in written if not p.startswith(unused_dir)]
                logger.info(
                    "emitter_pruned_unused_module",
                    extra={"service_name": spec_name},
                )

    return written


def _scan_referenced_modules(env_layout_root: str) -> Set[str]:
    """Walk every emitted env's main.tf and return the set of module
    service_names referenced via `source = "../../modules/<name>"`."""
    refs: Set[str] = set()
    if not os.path.isdir(env_layout_root):
        return refs
    pat = re.compile(r'source\s*=\s*"[\.\/]+modules/([a-zA-Z0-9_\-]+)"?')
    for root, _dirs, files in os.walk(env_layout_root):
        for fname in files:
            if fname != "main.tf":
                continue
            full = os.path.join(root, fname)
            try:
                with open(full, "r", encoding="utf-8") as fh:
                    content = fh.read()
            except OSError:
                continue
            for m in pat.finditer(content):
                refs.add(m.group(1))
    return refs


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
    customer_profile: str = "default",
) -> str:
    """One module {} block per source resource. Order: stable by module_path then name."""
    from migrator.translate.compliance_profiles import list_services_hardened_by
    _hardened_globally = set(list_services_hardened_by(compliance_profile))
    # Compliance-profile tokens (alb / eks / rds / s3 / secrets / vpc) don't
    # match translator service_names 1:1 — eks-cluster / aurora-postgres /
    # s3-bucket / secretsmanager-secret etc. emit longer slugs. Aliases let
    # the header's "Hardened defaults applied to" claim reflect REAL
    # coverage instead of just the exact-token matches. Kiro v9 #6.
    _HARDENED_TOKEN_TO_SERVICES: Dict[str, tuple] = {
        "alb":     ("alb",),
        "eks":     ("eks-cluster",),
        "rds":     ("rds-postgres", "rds-mysql", "aurora-postgres"),
        "s3":      ("s3-bucket",),
        "secrets": ("secretsmanager-secret", "secrets-manager-secret"),
        "vpc":     ("vpc",),
    }

    # Build the rest of the body FIRST so we can intersect the
    # globally-hardenable service list with the ones actually emitted
    # in this env. Without the intersection the header listed services
    # this env didn't even contain — misleading per Kiro v8 review.
    # Header lines get prepended at the end once `services_actually_emitted`
    # is known.
    lines: List[str] = []
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

    # Two-pass strategy for cross-module wiring:
    # PASS 1: translate every resource → collect (block_name, service_name)
    #         pairs and stash the per-resource translation output.
    # PASS 2: render each module call, using the FULL set of services-in-env
    #         to rewrite cross-module references (vpc_id, subnet_ids,
    #         ssl_certificate_arn, target_arn). Replaces TODOs with
    #         module.X.Y references where the provider module is in this env.
    from .cross_module_wiring import (
        rewrite_inputs as _wire_rewrite,
        extract_top_level_map_keys as _wire_extract_keys,
        _WIRING_RULES as _wire_rules,
    )

    used_names: Set[str] = set()
    per_resource: List[Dict[str, Any]] = []
    modules_in_env: List[tuple] = []   # [(block_name, service_name), ...]
    # Per-block extracted output-map keys, used by the named-lookup
    # wiring path. Shape: { block_name: { input_map_name: [keys...] } }
    # Example: { "compute_network_vpc": { "vpcs": ["vpc_nfr_shared",
    # "vpc_demo_shared", ...] } }. Populated in PASS 1, consumed in PASS 2.
    provider_output_keys: Dict[str, Dict[str, List[str]]] = {}

    # ---- PASS 1: translate + assign block names ----
    for r in resources:
        conf = confidence_by_addr.get(r.address) or confidence_by_type.get(r.tf_type)
        translation = _translate_terraform_resource(
            r, source_iac=source_iac, compliance_profile=compliance_profile,
        )

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

        base_name = _safe_identifier(f"{r.tf_type.replace('google_', '')}_{r.name}")
        block_name = base_name
        counter = 1
        while block_name in used_names:
            counter += 1
            block_name = f"{base_name}_{counter}"
        used_names.add(block_name)

        # Record the (block_name, service_name) pair for the wiring pass.
        if has_translation:
            modules_in_env.append((block_name, translation.service_name))
            # Extract output keys for any input maps the wiring layer
            # cares about (e.g., vpc → "vpcs", sns-sqs-fanout →
            # "topics", acm-certificate → "certificates"). These let
            # wiring emit module.X.Y["key"] instead of values()[0].
            input_maps_for_service = {
                rule.provider_input_map
                for rule in _wire_rules
                if rule.provider_service == translation.service_name
                and rule.provider_input_map
            }
            if input_maps_for_service:
                provider_output_keys[block_name] = {}
                for attr in input_maps_for_service:
                    keys = _wire_extract_keys(
                        translation.aws_inputs_hcl, attr,
                    )
                    if keys:
                        provider_output_keys[block_name][attr] = keys

        per_resource.append({
            "resource":         r,
            "conf":             conf,
            "translation":      translation,
            "block_name":       block_name,
            "has_translation":  has_translation,
        })

    # ---- PASS 2: render with cross-module wiring applied ----
    for entry in per_resource:
        r          = entry["resource"]
        conf       = entry["conf"]
        translation = entry["translation"]
        block_name  = entry["block_name"]
        has_translation = entry["has_translation"]

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
            inputs_body = _sanitize_translation(
                translation.aws_inputs_hcl, customer_profile=customer_profile,
            ).rstrip()
            # NEW: cross-module wiring — replace `vpc_id = "TODO-vpc-id"`,
            # `subnet_ids = []`, `ssl_certificate_arn = "TODO-acm-cert-arn"`
            # etc. with module.X.Y references when the provider module
            # exists in this env. Wiring rules table is in
            # cross_module_wiring.py.
            #
            # consumer_block_name lets the wiring layer pick the
            # closest-named provider when there are multiple of the
            # same service in the env (DH's common-network has 8 VPC
            # modules — Kiro's review fix #5).
            # provider_output_keys lets wiring emit named-key lookups
            # `module.X.Y["specific_key"]` instead of values()[0] when
            # the chosen provider has multiple output keys (Kiro v3 #2+#3).
            inputs_body = _wire_rewrite(
                inputs_body,
                modules_in_env=modules_in_env,
                consumer_block_name=block_name,
                provider_output_keys=provider_output_keys,
            )
            if inputs_body:
                lines.append(inputs_body)
            lines.append("}")
            lines.append("")
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

    # Header lines — built last so the "Hardened defaults applied to:"
    # list intersects the globally-hardenable services with the ones
    # actually emitted in this env. Kiro v8 review caught the previous
    # all-globals listing as misleading (common_common_network header
    # claimed eks + rds hardening despite emitting neither module).
    services_actually_emitted = {svc for _blk, svc in modules_in_env}
    # Intersect via the alias map so e.g. token "eks" matches when the
    # env emits service_name "eks-cluster". Without aliases, only
    # exact-match tokens like "alb" and "vpc" surfaced — Kiro v9 #6
    # caught terarecon's header claiming only "alb" hardened despite
    # eks-cluster + aurora-postgres + s3-bucket all having HIPAA
    # defaults applied.
    services_hardened_here = sorted([
        tok for tok in _hardened_globally
        if any(
            svc in services_actually_emitted
            for svc in _HARDENED_TOKEN_TO_SERVICES.get(tok, (tok,))
        )
    ])
    header: List[str] = []
    header.append(f"# AWS Terraform root for env={env_name}")
    header.append("# Synthesized by Cloud Lifecycle Intelligence — Migrator engine.")
    header.append("# Each module {} block below corresponds to one source GCP resource.")
    header.append("# Review per-resource source comments; replace TODO inputs before plan.")
    if compliance_profile and compliance_profile != "none":
        header.append("#")
        header.append(f"# Compliance profile: {compliance_profile.upper()}")
        if services_hardened_here:
            header.append(
                f"# Hardened defaults applied to: {', '.join(services_hardened_here)}"
            )
            header.append("# (e.g. block_public_access, KMS encryption, deletion_protection)")
        else:
            header.append(
                f"# This env emits no modules that {compliance_profile.upper()} "
                f"hardens (would harden: {', '.join(sorted(_hardened_globally)) or '(none)'} "
                "— but none of those modules are in this env)."
            )
    else:
        header.append("# Compliance profile: none (operator hardens each resource manually)")
    header.append("")

    return "\n".join(header + lines) + "\n"


# Description text for each known cross-env variable. Keeps the
# generated variables.tf self-documenting so operators understand
# why a `var.X` placeholder exists and how to wire it.
@dataclass(frozen=True)
class _CrossEnvVarSpec:
    """Type + default + description for a cross-env variable.

    The wiring layer emits `var.<name>` references; this struct lets
    variables.tf declare each one with the right Terraform type so
    `terraform validate` stays green. e.g., subnet_ids needs
    `list(string)`, not the default `string`.
    """
    type:         str
    default_hcl:  str   # raw HCL literal (used verbatim in `default = ...`)
    description:  str


_CROSS_ENV_VAR_SPECS: Dict[str, _CrossEnvVarSpec] = {
    "ssl_certificate_arn": _CrossEnvVarSpec(
        type="string",
        default_hcl='"TODO-supply-ssl_certificate_arn"',
        description=(
            "ACM certificate ARN for ALB listeners. Set in this env's "
            "tfvars when the cert is provisioned in a different env "
            "(e.g., a shared 'certificate-manager' env) — Migrator emits "
            "this placeholder when no in-env ACM module is available to "
            "wire automatically."
        ),
    ),
    "vpc_id": _CrossEnvVarSpec(
        type="string",
        default_hcl='"TODO-supply-vpc_id"',
        description=(
            "VPC ID. Required when this env's consumers (EC2 / EKS / ALB / "
            "RDS / etc.) live in a SHARED VPC defined in a different env "
            "(common pattern for satellite envs like DH's terarecon). "
            "Supply via tfvars or remote_state lookup of the shared VPC env's outputs."
        ),
    ),
    "subnet_ids": _CrossEnvVarSpec(
        type="list(string)",
        default_hcl="[]",   # empty list keeps validate green; tfvars overrides
        description=(
            "List of subnet IDs in the shared VPC. Required when this env "
            "uses a cross-env VPC (see vpc_id description). Supply as a "
            "list of subnet IDs via tfvars or remote_state lookup."
        ),
    ),
    "public_subnet_ids": _CrossEnvVarSpec(
        type="list(string)",
        default_hcl="[]",
        description=(
            "Public subnet IDs for NAT Gateway placement (one per AZ for "
            "HA). The VPC module doesn't tag subnets as public vs private "
            "yet, so the operator supplies these explicitly via tfvars. "
            "Empty list disables the NAT Gateway."
        ),
    ),
    "private_subnet_route_table_ids": _CrossEnvVarSpec(
        type="list(string)",
        default_hcl="[]",
        description=(
            "Private subnet route table IDs that should egress via the "
            "NAT Gateway. Order must match public_subnet_ids. Without "
            "this the NAT Gateway is created but private resources have "
            "no egress route through it."
        ),
    ),
    "query_results_bucket": _CrossEnvVarSpec(
        type="string",
        default_hcl='"TODO-supply-athena-query-results-bucket"',
        description=(
            "S3 bucket NAME (not ARN) that Athena writes query results to. "
            "Wired automatically to the first emitted S3 bucket when this "
            "env has an s3-bucket module; otherwise supplied here for "
            "operators to set via tfvars."
        ),
    ),
    "firehose_destination_bucket": _CrossEnvVarSpec(
        type="string",
        default_hcl='"TODO-supply-firehose-destination-bucket"',
        description=(
            "S3 bucket NAME (not ARN) that Kinesis Firehose delivers log "
            "records to. Auto-wired to the first emitted S3 bucket when "
            "this env has an s3-bucket module; otherwise supply via tfvars."
        ),
    ),
}

# Legacy alias — kept for the _scan_cross_env_vars iteration path. Maps
# var name → human description so existing callers keep working.
_CROSS_ENV_VAR_DESCRIPTIONS: Dict[str, str] = {
    name: spec.description for name, spec in _CROSS_ENV_VAR_SPECS.items()
}


# Strict-format fields where a literal TODO string fails plan with
# obscure AWS API errors. When the source has an unresolvable local
# (`cidr = local.X`) and the surgical sanitizer leaves
# `cidr = "TODO-local-X"`, plan fails with "invalid CIDR block
# format" — operator has no idea what to do. Promote to a required
# variable so plan fails earlier with the cleaner error "Variable
# subnet_cidr_X is required" + description naming the source local.
# Kiro v9 Gap 1+2 polish.
_STRICT_FIELD_PATTERN = re.compile(
    # Matches `<field> = "TODO-(local|var|each|dependency)-<slug>"`
    # anywhere on a line — handles both top-level attributes AND
    # nested object literals like `{ dns_name = "TODO-X", visibility = "..." }`.
    # Captures (prefix-with-whitespace, field, todo-slug).
    r'(^|\s|[{,])\s*(cidr|dns_name|ip_address|allocation_id|certificate_arn|kms_key_arn|target_arn|role_arn|arn|public_ip|private_ip)\s*=\s*"(TODO-(?:local|var|each|dependency)-[\w.\-]+)"',
    re.MULTILINE,
)

# Module-level cache of promoted var descriptions. Populated during
# emission for each env; consumed by _render_env_variables_tf.
_PROMOTED_VAR_DESCRIPTIONS: Dict[str, str] = {}


def _promote_strict_field_todos(rendered_main_tf: str) -> "tuple[str, Dict[str, str]]":
    """Scan ``rendered_main_tf`` for literal TODO values in strict-format
    fields and rewrite them as ``var.<safe_name>`` references.

    Returns the rewritten HCL + a dict {var_name: description} of the
    variables that need declaring in variables.tf. The descriptions
    name the source local so operators see exactly what to supply.
    """
    promoted: Dict[str, str] = {}

    def repl(m: "re.Match[str]") -> str:
        prefix, field, todo_slug = m.group(1), m.group(2), m.group(3)
        # Build a stable, identifier-safe var name from the field name
        # + the TODO slug.
        # e.g., `cidr` + `TODO-local-secondary_subnet_cfgs-primary-subnet-1-ip_cidr_range`
        # → `cidr_local_secondary_subnet_cfgs_primary_subnet_1_ip_cidr_range`
        suffix = re.sub(r"[^A-Za-z0-9_]+", "_", todo_slug.lower()).strip("_")
        var_name = f"{field}_{suffix}"[:200]   # cap length
        # Build a description that names the original source reference
        # so operator knows exactly which local/var to supply.
        # Escape `${` as `$$ {` (terraform's literal-dollar syntax) so
        # the description doesn't get interpreted as an interpolation.
        source_ref = todo_slug[len("TODO-"):].replace("-", ".", 1).replace("-", "_")
        desc = (
            f"Required value for {field}. Source had `{field} = "
            f"$${{{source_ref}}}` which cannot be resolved at parse time. "
            f"Supply via tfvars."
        )
        promoted[var_name] = desc
        return f"{prefix}{field} = var.{var_name}"

    rewritten = _STRICT_FIELD_PATTERN.sub(repl, rendered_main_tf)
    # Stash descriptions in the module-level cache so
    # _render_env_variables_tf can pick them up when rendering this env.
    _PROMOTED_VAR_DESCRIPTIONS.update(promoted)
    return rewritten, promoted


def _scan_cross_env_vars(rendered_main_tf: str) -> List[str]:
    """Find which `var.X` placeholders the wiring layer injected into
    this env's main.tf. Returns the set of var names (de-duped, sorted)
    that have matching cross-env descriptions and therefore need a
    `variable {}` block emitted in variables.tf.

    Pure string scan — keeps the emitter cycle-free (rendering main.tf
    doesn't need to know about variables.tf and vice-versa).
    """
    import re as _re
    found: Set[str] = set()
    for var_name in _CROSS_ENV_VAR_DESCRIPTIONS.keys():
        pat = _re.compile(rf"\bvar\.{_re.escape(var_name)}\b")
        if pat.search(rendered_main_tf):
            found.add(var_name)
    return sorted(found)


def _render_env_variables_tf(
    aws_region: str,
    cross_env_vars: Optional[List[str]] = None,
) -> str:
    """Render variables.tf for one env root.

    Always emits aws_account_id + region. When the env's main.tf has
    `var.X` placeholders injected by the cross_module_wiring layer
    (because the provider module wasn't in this env), additional
    `variable {}` blocks are added so terraform validate stays green
    and the operator has a clear knob to wire via tfvars / workspaces.
    """
    parts: List[str] = [
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
    ]
    for v in cross_env_vars or []:
        spec = _CROSS_ENV_VAR_SPECS.get(v)
        # Critical design choice (Kiro v7 fix #4+#6+#7): cross-env
        # variables are declared `nullable = false` with NO default.
        # That makes them REQUIRED — terraform plan fails with a clear
        # "variable X is required" error if the operator hasn't supplied
        # one. The earlier "default = TODO-supply-X" pattern passed
        # validate but produced literal-TODO values at apply time, which
        # then failed at the AWS API call with cryptic errors.
        if spec is None:
            # Check if this is a promoted strict-field TODO (Kiro v9
            # Gap 1+2 polish). Those carry custom descriptions naming
            # the source local that needs resolution.
            promoted_desc = _PROMOTED_VAR_DESCRIPTIONS.get(v)
            description = (
                promoted_desc
                if promoted_desc
                else "Cross-env reference. Must be supplied via tfvars or -var."
            )
            # Best-effort type inference: list-y fields get list type.
            vtype = (
                "list(string)" if v.startswith(("subnet_ids", "public_subnet_ids", "private_subnet_route_table"))
                else "string"
            )
            parts.append(
                "\n"
                f'variable "{v}" {{\n'
                f"  type        = {vtype}\n"
                f'  description = "{description}"\n'
                f"  nullable    = false\n"
                "}\n"
            )
            continue
        parts.append(
            "\n"
            f'variable "{v}" {{\n'
            f"  type        = {spec.type}\n"
            f'  description = "{spec.description}"\n'
            f"  nullable    = false\n"
            "}\n"
        )
    return "".join(parts)


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


# Per-service set of NON-aws providers that the module body uses.
# When any of these services is emitted in an env, the matching
# providers MUST be declared in the env-root versions.tf otherwise
# `terraform init` fails with "provider not found".
# Kiro v7 review caught this: aurora-postgres uses `resource
# "random_password"` (hashicorp/random) and eks-cluster uses
# `data "tls_certificate"` (hashicorp/tls).
_EXTRA_PROVIDERS_BY_SERVICE: Dict[str, tuple] = {
    "aurora-postgres":  ("random",),
    "eks-cluster":      ("tls", "random"),
    # add new entries here whenever a module body adopts a non-aws provider
}

_PROVIDER_BLOCK = {
    "random": (
        '    random = {\n'
        '      source  = "hashicorp/random"\n'
        '      version = "~> 3.6"\n'
        '    }\n'
    ),
    "tls": (
        '    tls = {\n'
        '      source  = "hashicorp/tls"\n'
        '      version = "~> 4.0"\n'
        '    }\n'
    ),
}


def _render_env_versions_tf(
    services_used: Optional[List[str]] = None,
) -> str:
    """Render the env-root versions.tf.

    Always includes `aws`. When the env emits modules requiring extra
    providers (random/tls for aurora/eks), declare those too so
    terraform init succeeds.
    """
    needed: Set[str] = set()
    for svc in services_used or []:
        for p in _EXTRA_PROVIDERS_BY_SERVICE.get(svc, ()):
            needed.add(p)

    providers = (
        "    aws = {\n"
        '      source  = "hashicorp/aws"\n'
        '      version = "~> 5.20"\n'
        "    }\n"
    )
    for p in sorted(needed):
        providers += _PROVIDER_BLOCK[p]

    return (
        "terraform {\n"
        '  required_version = ">= 1.5.0, < 2.0.0"\n'
        "  required_providers {\n"
        f"{providers}"
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
