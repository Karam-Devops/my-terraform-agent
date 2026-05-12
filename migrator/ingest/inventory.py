"""Build a flat list of DiscoveredResource entries from a parsed repo.

Two modes:

  * **Terraform mode** (vanilla .tf files present): extract `resource`
    blocks directly from each .tf file. This is the high-fidelity path.

  * **Terragrunt mode** (no .tf files; modules are referenced via
    `terraform { source = "git::..." }` in terragrunt.hcl): the actual
    resource declarations live in an external repo we don't have. We
    fall back to inferring resource types from the module-path string,
    using a heuristic table. Each terragrunt.hcl stack contributes one
    synthetic DiscoveredResource named after the stack's directory.

Downstream stages (plan, output) only consume DiscoveredResource — they
don't know or care which mode produced them.
"""

from __future__ import annotations

import os
import re
from typing import Any, Dict, List, Tuple

from migrator.results import DiscoveredResource

from .hcl_parser import extract_resource_blocks, parse_file
from .repo_walker import WalkResult


def build_inventory(walk: WalkResult) -> Tuple[List[DiscoveredResource], List[str]]:
    """Parse every IaC file in `walk` and return a flat resource list.

    Returns:
        (resources, errors)
            resources — every DiscoveredResource the engine could derive
            errors    — per-file parse failures (one-liner each)
    """
    resources: List[DiscoveredResource] = []
    errors: List[str] = []

    # ------------------------------------------------------------------
    # Pass 1: vanilla Terraform .tf files (real resource blocks).
    # ------------------------------------------------------------------
    for tf_path in walk.tf_files:
        ast, parse_errs = parse_file(tf_path)
        errors.extend(parse_errs)
        if not ast:
            continue

        module_path = _module_path_for(tf_path, walk.repo_root)
        for block in extract_resource_blocks(ast):
            resources.append(DiscoveredResource(
                tf_type=block["tf_type"],
                name=block["name"],
                module_path=module_path,
                file_path=tf_path,
                arguments=block.get("arguments", {}),
            ))

    # ------------------------------------------------------------------
    # Pass 2: Terragrunt synthetic resources.
    #
    # If the walk surfaced terragrunt.hcl files, each leaf stack is
    # treated as a synthetic resource whose tf_type is inferred from
    # the `terraform { source = ... }` URL or path string. This is
    # how we surface meaningful inventory for repos whose modules
    # live in an external GitLab/GitHub source we can't read.
    # ------------------------------------------------------------------
    for tg_path in walk.terragrunt_files:
        ast, parse_errs = parse_file(tg_path)
        errors.extend(parse_errs)
        if not ast:
            continue

        # Skip the *root* terragrunt.hcl (it has remote_state +
        # generate blocks, no terraform.source pointing at a module).
        # And skip _envcommon includes (they're library files, not
        # leaf stacks). Heuristic: if the file contains a
        # `terraform.source` we treat it as a leaf; otherwise skip.
        source_url = _extract_terraform_source(ast)
        if not source_url:
            continue

        # Synthesize: the stack's directory becomes the "name" and
        # module_path; the inferred GCP tf_type is the heuristic
        # mapping of the source URL.
        stack_dir = os.path.dirname(tg_path)
        rel_module_path = _module_path_for(tg_path, walk.repo_root)
        stack_name = _stack_name_from_dir(stack_dir)
        tf_type = infer_gcp_type_from_module_path(source_url)

        # Capture the inputs block (if any) as the "arguments" so
        # downstream tooling can show what the operator configured.
        inputs = _extract_inputs(ast)

        # Annotate with the source URL so the UI / migration guide can
        # show the operator exactly what module was being referenced.
        inputs.setdefault("_terragrunt_source", source_url)

        # Extract `dependencies { paths = [...] }` so the dep graph
        # can render real edges in Terragrunt mode (where there are
        # no inline <tf_type>.<name>.<attr> references inside .tf files).
        terragrunt_deps = _extract_dependency_paths(ast)

        resources.append(DiscoveredResource(
            tf_type=tf_type,
            name=stack_name,
            module_path=rel_module_path,
            file_path=tg_path,
            arguments=inputs,
            terragrunt_deps=terragrunt_deps,
        ))

    # Stable order for deterministic output.
    resources.sort(key=lambda r: (r.module_path, r.tf_type, r.name))
    return resources, errors


# -----------------------------------------------------------------
# Terragrunt-mode helpers
# -----------------------------------------------------------------

# Most module paths look like:
#   "${local._project.locals._gitlab_base_path}//GCP//resource-api//facility-groups//lb//net-lb-app-ext?ref=v1.2.5"
# We extract the segment after the last `/`
# and before any `?ref=...`. The pin regex is permissive — `?ref=` can
# be followed by literal versions (`v1.2.5`) OR by interpolations
# (`${local._project.locals._version}`), so we strip everything after.
_REF_PIN_RE = re.compile(r"\?ref=.*$")

# Strip `${...}` interpolation tokens that may appear inside the path
# segment too (the customer occasionally embeds version pins as
# subpath segments). After stripping interpolations the path is
# usually clean.
_INTERP_RE = re.compile(r"\$\{[^}]+\}")
# Strip git-protocol prefixes so the rule matcher sees the actual path.
_GIT_PROTO_RE = re.compile(r"^(git::|http://|https://|ssh://)", re.IGNORECASE)


def _extract_terraform_source(ast: Dict[str, Any]) -> str:
    """Pull the `terraform { source = ... }` value out of a parsed AST.

    Returns the raw string (with `${...}` interpolations intact). Empty
    string if not present.
    """
    tf_blocks = ast.get("terraform", []) or []
    for block in tf_blocks:
        if not isinstance(block, dict):
            continue
        src = block.get("source")
        if isinstance(src, str) and src:
            return src
    return ""


def _extract_inputs(ast: Dict[str, Any]) -> Dict[str, Any]:
    """Pull `inputs = { ... }` out of a Terragrunt AST.

    Terragrunt's `inputs` is a top-level attribute (not a block), so
    python-hcl2 surfaces it as ast["inputs"] = [{...}] (list-wrapped).
    """
    raw = ast.get("inputs")
    if isinstance(raw, list) and raw:
        if isinstance(raw[0], dict):
            return dict(raw[0])
    if isinstance(raw, dict):
        return dict(raw)
    return {}


def _extract_dependency_paths(ast: Dict[str, Any]) -> List[str]:
    """Pull relative paths out of a Terragrunt `dependencies { paths = [...] }` block.

    python-hcl2 emits this as:
        ast["dependencies"] = [{"paths": ["../cloud-armor", "../dns"]}]

    Returns an empty list if the block is missing or malformed.
    """
    deps_blocks = ast.get("dependencies", []) or []
    out: List[str] = []
    for block in deps_blocks:
        if not isinstance(block, dict):
            continue
        paths = block.get("paths")
        if isinstance(paths, list):
            for p in paths:
                if isinstance(p, str) and p:
                    out.append(p)
    return out


def _stack_name_from_dir(stack_dir: str) -> str:
    """Last directory segment, sanitized for use as an HCL identifier.

    For path `live/prod/us-central1/networking` returns `networking`.
    For `live/prod/us-central1/lb/net-lb-app-ext` returns `net_lb_app_ext`
    (the directory layout sometimes goes deeper).
    """
    name = os.path.basename(os.path.normpath(stack_dir))
    return name.replace("-", "_") or "unknown_stack"


# Keyword → GCP tf_type lookup. Order matters: more specific patterns
# come first so e.g. `cloud-sql` doesn't fall through to `sql` doesn't
# match before `cloud-sql`. Substring match against the lowercased
# module path string.
_INFER_RULES = (
    # ---------- Networking ----------
    ("cloud-armor",                    "google_compute_security_policy"),
    # DH customer uses British spelling — synonym match
    ("cloud-armour",                   "google_compute_security_policy"),
    ("network-connectivity",           "google_network_connectivity_hub"),
    ("ncc-hub",                        "google_network_connectivity_hub"),
    ("ncc-spoke",                      "google_network_connectivity_spoke"),

    # Shared VPC (GCP's host-project + service-project pattern) — no
    # direct AWS equivalent. Maps to Transit Gateway hub-spoke (or RAM
    # subnet share). See coverage.py for migration strategies.
    ("shared-vpc-host",                "google_compute_shared_vpc_host_project"),
    ("shared-vpc-service",             "google_compute_shared_vpc_service_project_attachment"),
    ("host-project",                   "google_compute_shared_vpc_host_project"),
    ("service-project-attach",         "google_compute_shared_vpc_service_project_attachment"),
    # DH-specific path segment for compute address allocation (one
    # leaf stack per env that bundles internal + global static IPs).
    # Maps to google_compute_address (handled by EIP translator).
    ("net-address",                    "google_compute_address"),
    ("forwarding-rule",                "google_compute_global_forwarding_rule"),
    ("net-lb",                         "google_compute_global_forwarding_rule"),
    ("load-balancer",                  "google_compute_global_forwarding_rule"),
    ("lb-with-http",                   "google_compute_global_forwarding_rule"),
    ("alb-net",                        "google_compute_global_forwarding_rule"),
    ("/lb/",                           "google_compute_global_forwarding_rule"),
    ("backend-service",                "google_compute_backend_service"),
    ("armor-module",                   "google_compute_security_policy"),
    ("armor",                          "google_compute_security_policy"),
    ("health-check",                   "google_compute_health_check"),
    ("ssl-cert",                       "google_compute_ssl_certificate"),
    ("ssl-policy",                     "google_compute_ssl_policy"),
    ("global-address",                 "google_compute_global_address"),
    ("static-ip",                      "google_compute_address"),
    ("nat",                            "google_compute_router_nat"),
    ("router",                         "google_compute_router"),
    ("subnet",                         "google_compute_subnetwork"),
    ("firewall",                       "google_compute_firewall"),
    ("dns-managed-zone",               "google_dns_managed_zone"),
    ("dns-record",                     "google_dns_record_set"),
    ("dns",                            "google_dns_managed_zone"),
    ("vpc-access-connector",           "google_vpc_access_connector"),
    # VPN classification MUST come before vpc/network — customer's source
    # has paths like `common-network/network/net-vpn/net-vpn-ha` where the
    # `network` substring would otherwise match first (false positive:
    # VPN module classified as VPC).
    ("net-vpn-ha",                     "google_compute_vpn_tunnel"),
    ("net-vpn",                        "google_compute_vpn_tunnel"),
    ("aws-vpn",                        "google_compute_vpn_tunnel"),
    ("vpn-common-mgmt",                "google_compute_vpn_tunnel"),
    ("vpn-gateway",                    "google_compute_vpn_gateway"),
    ("vpn-tunnel",                     "google_compute_vpn_tunnel"),
    # Bare "vpn" segment — careful, this is broad. Only matches when
    # path contains "/vpn/" or ends in "/vpn".
    ("/vpn/",                          "google_compute_vpn_tunnel"),
    ("/vpn",                           "google_compute_vpn_tunnel"),
    ("vpc",                            "google_compute_network"),
    ("network",                        "google_compute_network"),

    # ---------- Compute ----------
    ("gke",                            "google_container_cluster"),
    ("kubernetes-cluster",             "google_container_cluster"),
    ("node-pool",                      "google_container_node_pool"),
    ("instance-template",              "google_compute_instance_template"),
    ("compute-instance",               "google_compute_instance"),
    ("vm",                             "google_compute_instance"),
    # DH customer's module repo uses `compute-disc` (a typo or shortened
    # spelling) instead of `compute-disk`. Add explicit rule so it
    # still classifies as a Compute Disk → AWS EBS.
    ("compute-disc",                   "google_compute_disk"),
    ("disk",                           "google_compute_disk"),
    ("cloud-run-v2",                   "google_cloud_run_v2_service"),
    ("cloud-run",                      "google_cloud_run_v2_service"),
    ("cloud-functions-v2",             "google_cloudfunctions2_function"),
    ("cloud-function",                 "google_cloudfunctions2_function"),
    ("cloud-functions",                "google_cloudfunctions2_function"),
    # DH-specific: `shared-infra/http` is their wrapper around an
    # HTTP-triggered Cloud Function. Dependencies (auth0, pubsub,
    # service-account, cloud-build/trigger) confirm Cloud Function shape.
    # Path-segment form so this doesn't false-match URLs like
    # `https://cloud.google.com/foo/http-load-balancer/...`.
    ("/shared-infra/http",             "google_cloudfunctions2_function"),

    # ---------- Data ----------
    ("cloud-sql",                      "google_sql_database_instance"),
    ("sql-database-instance",          "google_sql_database_instance"),
    ("sql-old-module",                 "google_sql_database_instance"),
    # sql-import-data is a one-off DATA IMPORT job that runs against an
    # existing Cloud SQL instance — NOT a database resource itself. Map
    # to a synthetic MANUAL_REVIEW type so the Aurora translator doesn't
    # fire on it (was producing TODO_cluster_name placeholders). AWS
    # equivalent: AWS DMS migration task OR aws_db_instance_role
    # (operator runs data load via psql/mysql client).
    ("sql-import-data",                "google_sql_import_job"),
    ("sql-import",                     "google_sql_import_job"),
    ("postgres",                       "google_sql_database_instance"),
    ("mysql",                          "google_sql_database_instance"),
    # Bare `sql` catch-all — must come AFTER more specific patterns
    # like `cloud-sql` (else `cloud-sql` would never match).
    ("sql",                            "google_sql_database_instance"),
    ("memorystore",                    "google_redis_instance"),
    ("redis",                          "google_redis_instance"),
    ("bigquery-dataset",               "google_bigquery_dataset"),
    ("bigquery-table",                 "google_bigquery_table"),
    ("bigquery",                       "google_bigquery_dataset"),
    ("storage-bucket",                 "google_storage_bucket"),
    ("gcs-bucket",                     "google_storage_bucket"),
    ("gcs",                            "google_storage_bucket"),
    ("filestore",                      "google_filestore_instance"),

    # ---------- IAM / security ----------
    ("kms-crypto-key",                 "google_kms_crypto_key"),
    ("kms-keyring",                    "google_kms_key_ring"),
    ("crypto-key",                     "google_kms_crypto_key"),
    ("kms",                            "google_kms_crypto_key"),
    ("service-account",                "google_service_account"),
    ("iam-binding",                    "google_project_iam_binding"),
    ("iam-member",                     "google_project_iam_member"),
    ("custom-role",                    "google_project_iam_custom_role"),
    ("workload-identity",              "google_service_account_iam_binding"),
    ("secret-manager",                 "google_secret_manager_secret"),
    ("secret",                         "google_secret_manager_secret"),
    ("certificate-manager",            "google_certificate_manager_certificate"),
    ("cert-manager",                   "google_certificate_manager_certificate"),

    # ---------- Messaging ----------
    ("pubsub-subscription",            "google_pubsub_subscription"),
    ("pubsub-topic",                   "google_pubsub_topic"),
    ("pubsub",                         "google_pubsub_topic"),

    # ---------- Container / registry ----------
    ("artifact-registry",              "google_artifact_registry_repository"),
    ("gar",                            "google_artifact_registry_repository"),

    # ---------- Observability ----------
    ("logging-sink",                   "google_logging_project_sink"),
    ("log-sink",                       "google_logging_project_sink"),
    ("logging-bucket",                 "google_logging_project_bucket_config"),
    # DH source alias for the same resource type
    ("log-bucket",                     "google_logging_project_bucket_config"),
    ("monitoring-alert",               "google_monitoring_alert_policy"),
    ("alert-policy",                   "google_monitoring_alert_policy"),
    ("uptime-check",                   "google_monitoring_uptime_check_config"),
    ("notification-channel",           "google_monitoring_notification_channel"),

    # ---------- Schedulers / orchestration ----------
    ("cloud-scheduler",                "google_cloud_scheduler_job"),
    ("scheduler-job",                  "google_cloud_scheduler_job"),
    ("composer",                       "google_composer_environment"),
    ("airflow",                        "google_composer_environment"),
    ("dataflow",                       "google_dataflow_job"),
    ("dataform",                       "google_dataform_repository"),

    # ---------- CI/CD (Cloud Build family) ----------
    # Order matters: longest/most-specific cloud-build path first.
    ("cloud-build/repository",         "google_cloudbuildv2_repository"),
    ("cloud-build/trigger",            "google_cloudbuild_trigger"),
    ("cloud-build/worker-pool",        "google_cloudbuild_worker_pool"),
    # Generic last-segment matches (when path doesn't include
    # `cloud-build/` prefix but the leaf clearly indicates the type).
    ("worker-pool",                    "google_cloudbuild_worker_pool"),
    ("cloud-build",                    "google_cloudbuild_trigger"),
    # `trigger` and `repository` are too broad on their own, but match
    # path-segment forms like `/trigger` / `/repository` to scope them.
    ("/trigger",                       "google_cloudbuild_trigger"),
    ("/repository",                    "google_cloudbuildv2_repository"),

    # ---------- Async work / queues ----------
    ("cloud-tasks",                    "google_cloud_tasks_queue"),
    ("cloud-task",                     "google_cloud_tasks_queue"),

    # ---------- Data (AlloyDB) ----------
    ("alloy-db",                       "google_alloydb_instance"),
    ("alloydb",                        "google_alloydb_instance"),

    # ---------- IAM / Org hierarchy ----------
    ("folder-iam",                     "google_folder_iam_binding"),
    ("project-iam-access",             "google_project_iam_member"),
    ("project-level-access",           "google_project_iam_member"),
    ("project-org-policy",             "google_org_policy_policy"),
    ("project-tags",                   "google_tags_tag_value"),
    ("tag/data",                       "google_tags_tag_value"),
    ("api-activation",                 "google_project_service"),
    # `project` is broad — only match path-segment form so dirs like
    # `prj-dh-n-dev-os-01` (with `project` inside) don't false-match.
    # Customer's source has `/shared-infra/project/` patterns.
    ("/project/",                      "google_project"),
    ("/project",                       "google_project"),
    ("multiple-projects",              "google_project"),

    # ---------- Project data sources (cross-stack reads) ----------
    # `projects-data` is DH's pattern for `data "google_project" "X"`
    # blocks that read OTHER projects' attributes (project_number, etc.)
    # for cross-project IAM/networking. No AWS resource equivalent —
    # AWS data sources read accounts via aws_caller_identity.
    ("projects-data",                  "google_project_data_source"),

    # ---------- Anthos Service Mesh (GCP-specific paradigm) ----------
    ("asm-setup",                      "google_gke_hub_feature"),
    ("anthos-service-mesh",            "google_gke_hub_feature"),

    # ---------- Service quota tweaks ----------
    ("quota-adjuster",                 "google_service_usage_consumer_quota_override_v1beta"),

    # ---------- 3rd-party providers (Auth0 / Octopus / Workspace) ----------
    # These are non-GCP providers customers wire into the same
    # Terragrunt repo. They classify as MANUAL_REVIEW (no GCP→AWS
    # path) but inference here surfaces them with a clean type label
    # instead of "unknown_X". Coverage.py adds matching _MANUAL_REVIEW
    # entries with explicit reasons.
    ("auth0",                          "auth0_provider"),
    ("octopus",                        "octopusdeploy_resource"),
    ("google-drive",                   "googleworkspace_drive_folder"),

    # ---------- Specials Kiro flags as MANUAL_REVIEW ----------
    ("apigee-organization",            "google_apigee_organization"),
    ("apigee-environment",             "google_apigee_environment"),
    ("apigee-instance",                "google_apigee_instance"),
    ("apigee",                         "google_apigee_organization"),
    ("firestore",                      "google_firestore_database"),
    ("firebase",                       "google_firestore_database"),
)


def infer_gcp_type_from_module_path(module_source: str) -> str:
    """Heuristic: GCP tf_type implied by a Terragrunt module source string.

    Used in Terragrunt mode where actual resource blocks live in an
    external module repo. We look for distinctive substrings in the
    module's URL/path and return our best guess.

    Falls back to ``unknown_<last-path-segment>`` when no rule matches —
    coverage.py treats `unknown_*` as MANUAL_REVIEW.
    """
    p = (module_source or "").lower()
    # Normalize: strip `${...}` interpolation tokens, git protocol
    # prefixes, and the `?ref=...` suffix so rule matching sees a
    # clean path-only string.
    p = _INTERP_RE.sub("", p)
    p = _GIT_PROTO_RE.sub("", p)
    p = _REF_PIN_RE.sub("", p)

    for needle, tf_type in _INFER_RULES:
        if needle in p:
            return tf_type

    # Fallback: the last meaningful segment becomes part of an
    # `unknown_*` synthetic type that downstream coverage.py will
    # surface as MANUAL_REVIEW.
    last_seg = p.rstrip("/").split("/")[-1] or "stack"
    last_seg = re.sub(r"[^a-z0-9_]+", "_", last_seg).strip("_") or "stack"
    return f"unknown_{last_seg}"


def _module_path_for(file_path: str, repo_root: str) -> str:
    """Repo-relative path of the directory containing this file."""
    abs_dir = os.path.dirname(os.path.abspath(file_path))
    abs_root = os.path.abspath(repo_root)
    try:
        return os.path.relpath(abs_dir, abs_root).replace(os.sep, "/")
    except ValueError:
        return abs_dir.replace(os.sep, "/")
