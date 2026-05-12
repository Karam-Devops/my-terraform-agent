"""GCP→AWS resource mapping + per-resource confidence scoring.

The mapping table below is seeded from Kiro Power's published GCP→AWS
analysis (see phase7_kiro_repo_scan project memory). Each entry encodes:
  * the AWS resource type that's the closest equivalent
  * a confidence percentage (0–100) reflecting Kiro's assessment of
    "how much of the source resource translates cleanly"
  * a band label (HIGH / MEDIUM / LOW / MANUAL_REVIEW)
  * a short operator-facing reason
  * caveats / known gaps

This is a starting baseline. Per-customer tuning belongs in a
``customer_overrides.py`` (future) so this module stays the public
default.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional

from migrator.results import (
    CONFIDENCE_HIGH,
    CONFIDENCE_LOW,
    CONFIDENCE_MANUAL,
    CONFIDENCE_MEDIUM,
    ConfidenceFinding,
    DiscoveredResource,
)


@dataclass(frozen=True)
class _MappingEntry:
    aws_equivalent: Optional[str]   # None == no AWS analog → MANUAL_REVIEW
    score_pct: int                  # 0–100
    reason: str
    notes: tuple = ()                # tuple-of-strings for hashability


def _band_for(score_pct: int) -> str:
    if score_pct >= 85:
        return CONFIDENCE_HIGH
    if score_pct >= 60:
        return CONFIDENCE_MEDIUM
    if score_pct > 0:
        return CONFIDENCE_LOW
    return CONFIDENCE_MANUAL


# -----------------------------------------------------------------
# GCP → AWS mapping table (Kiro-seeded baseline).
# Key = GCP tf_type. Order does not matter; lookup is by exact key.
# -----------------------------------------------------------------

_GCP_TO_AWS: Dict[str, _MappingEntry] = {
    # ---- HIGH confidence (85-95) ----
    "google_compute_network": _MappingEntry(
        aws_equivalent="aws_vpc",
        score_pct=90,
        reason="VPC topology translates directly; CIDR ranges + flow logs preserved.",
        notes=("Routing mode (REGIONAL/GLOBAL) has no direct AWS analog — defaults to single-region.",),
    ),
    "google_compute_subnetwork": _MappingEntry(
        aws_equivalent="aws_subnet",
        score_pct=90,
        reason="Subnet CIDR + secondary ranges + private Google access map cleanly.",
        notes=("AWS subnets are zonal; GCP subnets are regional — one GCP subnet may emit one AWS subnet per AZ.",),
    ),
    "google_compute_address": _MappingEntry(
        aws_equivalent="aws_eip",
        score_pct=88,
        reason="Static IP allocation + region map directly to Elastic IP.",
        notes=(),
    ),
    "google_compute_router": _MappingEntry(
        aws_equivalent="aws_internet_gateway",
        score_pct=85,
        reason="Cloud Router's BGP role partly fills an AWS Internet Gateway role for egress.",
        notes=("Cloud Router supports BGP for hybrid; AWS equivalent is Direct Connect Gateway / TGW.",),
    ),
    "google_compute_router_nat": _MappingEntry(
        aws_equivalent="aws_nat_gateway",
        score_pct=88,
        reason="Outbound NAT translates 1:1.",
        notes=("AWS NAT GW is per-AZ; GCP Cloud NAT is per-VPC — emit one NAT GW per AZ for HA.",),
    ),
    "google_compute_firewall": _MappingEntry(
        aws_equivalent="aws_security_group",
        score_pct=87,
        reason="Allow rules translate to SG ingress; deny rules need NACL.",
        notes=(
            "GCP firewall is VPC-attached; AWS SG is instance-attached — topology shift.",
            "Egress-default in GCP differs from AWS (AWS SG default-allow-egress).",
        ),
    ),
    "google_storage_bucket": _MappingEntry(
        aws_equivalent="aws_s3_bucket",
        score_pct=90,
        reason="Storage class, versioning, lifecycle rules, CORS map directly.",
        notes=("Uniform bucket-level access maps to S3 bucket policy + Block Public Access.",),
    ),
    "google_storage_bucket_iam_member": _MappingEntry(
        aws_equivalent="aws_s3_bucket_policy",
        score_pct=85,
        reason="Per-bucket IAM grant translates to bucket policy statement.",
        notes=("Multiple GCP iam_members on same bucket collapse to single AWS policy doc.",),
    ),
    "google_sql_database_instance": _MappingEntry(
        aws_equivalent="aws_db_instance",
        score_pct=92,
        reason="PostgreSQL/MySQL versions, tier, disk size, backup config translate cleanly.",
        notes=(
            "Cloud SQL HA (REGIONAL) → RDS Multi-AZ (different replication semantics).",
            "PSA private IP requires AWS RDS in the same VPC, no equivalent peering needed.",
        ),
    ),
    "google_sql_database": _MappingEntry(
        aws_equivalent="aws_db_instance",
        score_pct=85,
        reason="Database creation rolled into the parent RDS instance config.",
        notes=("Multiple GCP databases per instance → multiple AWS RDS databases (CREATE DATABASE in user_data).",),
    ),
    "google_redis_instance": _MappingEntry(
        aws_equivalent="aws_elasticache_replication_group",
        score_pct=88,
        reason="Memorystore Redis configuration maps to ElastiCache Redis cluster.",
        notes=(),
    ),
    "google_artifact_registry_repository": _MappingEntry(
        aws_equivalent="aws_ecr_repository",
        score_pct=88,
        reason="Docker / Helm registries map to ECR repos.",
        notes=("Cross-repo replication policies need separate AWS ECR replication config.",),
    ),
    "google_pubsub_topic": _MappingEntry(
        aws_equivalent="aws_sns_topic",
        score_pct=85,
        reason="Topic + retention + storage policy translate; subscriptions become SNS→SQS fan-out.",
        notes=("GCP single-resource topic+sub model splits into AWS SNS topic + SQS queue per subscription.",),
    ),
    "google_pubsub_subscription": _MappingEntry(
        aws_equivalent="aws_sqs_queue",
        score_pct=85,
        reason="Subscription ack deadline + retention + retry policy map to SQS queue config.",
        notes=("Push subscriptions translate to SNS HTTP/Lambda subscriptions.",),
    ),
    "google_secret_manager_secret": _MappingEntry(
        aws_equivalent="aws_secretsmanager_secret",
        score_pct=85,
        reason="Secret name + replication policy map directly.",
        notes=("Secret versions migrate via the migration helper script.",),
    ),
    # NCC hub-spoke
    "google_network_connectivity_hub": _MappingEntry(
        aws_equivalent="aws_ec2_transit_gateway",
        score_pct=87,
        reason="Hub topology translates to TGW; spoke VPCs become TGW attachments.",
        notes=("STAR topology may need TGW route tables to enforce.",),
    ),
    "google_network_connectivity_spoke": _MappingEntry(
        aws_equivalent="aws_ec2_transit_gateway_vpc_attachment",
        score_pct=85,
        reason="Spoke VPC + linked services map to TGW VPC attachment.",
        notes=(),
    ),

    # ---- MEDIUM confidence (60-84) ----
    "google_container_cluster": _MappingEntry(
        aws_equivalent="aws_eks_cluster",
        score_pct=78,
        reason="Cluster networking + release channel translate; Workload Identity → IRSA needs SA email rewiring.",
        notes=(
            "master_ipv4_cidr_block has no direct EKS equivalent.",
            "Private cluster + master_authorized_networks → EKS endpoint config + SG.",
            "GKE Autopilot has no direct EKS equivalent — use EKS Fargate.",
        ),
    ),
    "google_container_node_pool": _MappingEntry(
        aws_equivalent="aws_eks_node_group",
        score_pct=78,
        reason="Node config (machine type, autoscaling, disk) translates; taints map cleanly.",
        notes=(
            "Preemptible nodes → EC2 Spot via launch-template capacity_type=SPOT.",
            "Workload metadata (GKE_METADATA) → IRSA (no direct equivalent setting).",
        ),
    ),
    "google_compute_instance": _MappingEntry(
        aws_equivalent="aws_instance",
        score_pct=75,
        reason="Machine type, boot disk, network interface translate.",
        notes=(
            "Service account (GCP) → instance profile (AWS) — different attachment model.",
            "Metadata (enable-oslogin, ssh-keys) → user_data + SSM.",
            "OS Login has no direct AWS equivalent — use SSM Session Manager.",
        ),
    ),
    "google_compute_disk": _MappingEntry(
        aws_equivalent="aws_ebs_volume",
        score_pct=82,
        reason="Disk type + size + zone map to EBS.",
        notes=("pd-balanced → gp3, pd-ssd → io2, pd-standard → gp2.",),
    ),
    "google_kms_key_ring": _MappingEntry(
        aws_equivalent=None,
        score_pct=70,
        reason="AWS KMS has no key-ring abstraction — collapses into the key.",
        notes=("Key ring becomes a tagging/naming convention on aws_kms_key.",),
    ),
    "google_kms_crypto_key": _MappingEntry(
        aws_equivalent="aws_kms_key",
        score_pct=80,
        reason="Symmetric encryption key + rotation period translate.",
        notes=(
            "GCP keyring + key (2 resources) → AWS KMS key (1 resource).",
            "Key policy replaces resource IAM bindings (different model).",
        ),
    ),
    "google_compute_global_address": _MappingEntry(
        aws_equivalent="aws_globalaccelerator_accelerator",
        score_pct=65,
        reason="Global anycast IPs partially translate — VPC peering use needs special handling.",
        notes=("PSA reservations have no direct AWS equivalent — use VPC peering or PrivateLink.",),
    ),
    "google_service_networking_connection": _MappingEntry(
        aws_equivalent=None,
        score_pct=55,
        reason="GCP PSA peering has no AWS-native equivalent — RDS uses subnet groups directly.",
        notes=("Replace with aws_db_subnet_group; managed-service VPC peering not needed in AWS.",),
    ),
    "google_compute_global_forwarding_rule": _MappingEntry(
        aws_equivalent="aws_lb",
        score_pct=72,
        reason="External HTTPS LB → ALB. CDN-fronted requires CloudFront.",
        notes=("NEG backends have no direct AWS equivalent — use TG with EKS service or ECS service.",),
    ),
    "google_compute_backend_service": _MappingEntry(
        aws_equivalent="aws_lb_target_group",
        score_pct=75,
        reason="Backend service → target group; health checks translate.",
        notes=(),
    ),
    "google_compute_health_check": _MappingEntry(
        aws_equivalent="aws_lb_target_group",
        score_pct=78,
        reason="Health-check parameters fold into ALB target group health_check block.",
        notes=(),
    ),
    "google_compute_ssl_certificate": _MappingEntry(
        aws_equivalent="aws_acm_certificate",
        score_pct=70,
        reason="Managed cert translates to ACM; manual upload also supported.",
        notes=("Cert validation method differs (DNS validation strongly preferred in ACM).",),
    ),
    "google_compute_ssl_policy": _MappingEntry(
        aws_equivalent="aws_lb_listener",
        score_pct=68,
        reason="SSL policy folds into ALB listener `ssl_policy` attribute.",
        notes=(),
    ),
    "google_dns_managed_zone": _MappingEntry(
        aws_equivalent="aws_route53_zone",
        score_pct=85,
        reason="Public/private managed zone maps directly to Route53.",
        notes=(),
    ),
    "google_dns_record_set": _MappingEntry(
        aws_equivalent="aws_route53_record",
        score_pct=85,
        reason="Record sets translate; routing policy options differ.",
        notes=(),
    ),
    "google_compute_security_policy": _MappingEntry(
        aws_equivalent="aws_wafv2_web_acl",
        score_pct=72,
        reason="Cloud Armor policy → AWS WAF v2 ACL. Rate limiting + geo-blocking translate.",
        notes=("Custom CEL rules need rewriting as AWS WAF JSON statements.",),
    ),
    "google_certificate_manager_certificate": _MappingEntry(
        aws_equivalent="aws_acm_certificate",
        score_pct=70,
        reason="Certificate Manager translates to ACM.",
        notes=(),
    ),
    "google_logging_project_sink": _MappingEntry(
        aws_equivalent="aws_kinesis_firehose_delivery_stream",
        score_pct=65,
        reason="Log routing translates to CloudWatch Logs subscription + Kinesis Firehose.",
        notes=("Filter expressions need rewriting (LQL → CloudWatch Logs Insights or filter pattern).",),
    ),
    "google_logging_project_bucket_config": _MappingEntry(
        aws_equivalent="aws_cloudwatch_log_group",
        score_pct=68,
        reason="Log retention + analytics maps to CloudWatch log group + retention.",
        notes=(),
    ),
    "google_monitoring_alert_policy": _MappingEntry(
        aws_equivalent="aws_cloudwatch_metric_alarm",
        score_pct=65,
        reason="Alert conditions translate; some metric paths differ.",
        notes=("MQL queries don't translate — needs rewrite to CloudWatch metric math.",),
    ),
    "google_monitoring_uptime_check_config": _MappingEntry(
        aws_equivalent="aws_route53_health_check",
        score_pct=72,
        reason="Uptime check translates to Route53 health check.",
        notes=("Or use CloudWatch Synthetics for richer behavioral checks.",),
    ),
    "google_monitoring_notification_channel": _MappingEntry(
        aws_equivalent="aws_sns_topic",
        score_pct=70,
        reason="Notification channel → SNS topic with email subscription.",
        notes=(),
    ),
    "google_cloud_scheduler_job": _MappingEntry(
        aws_equivalent="aws_scheduler_schedule",
        score_pct=65,
        reason="Cron jobs map to EventBridge Scheduler.",
        notes=(),
    ),

    # ---- LOW confidence (1-59) ----
    "google_service_account": _MappingEntry(
        aws_equivalent="aws_iam_role",
        score_pct=50,
        reason="Service account → IAM role. Workload Identity → IRSA needs full rewiring.",
        notes=(
            "GCP SAs are identities; AWS IAM roles are assumable. Different attachment model.",
            "All `serviceAccount:...@...iam.gserviceaccount.com` member references need rewriting.",
        ),
    ),
    "google_project_iam_member": _MappingEntry(
        aws_equivalent="aws_iam_role_policy_attachment",
        score_pct=45,
        reason="Resource-attached IAM binding → identity-attached policy. Topology shift.",
        notes=(
            "Granular AWS managed policies needed; not all GCP roles have direct equivalents.",
            "Custom roles need separate translation step.",
        ),
    ),
    "google_project_iam_binding": _MappingEntry(
        aws_equivalent="aws_iam_policy",
        score_pct=45,
        reason="Project IAM binding → IAM policy with role assumption statement.",
        notes=("Member-list semantics differ; AWS policies are document-based.",),
    ),
    "google_project_iam_custom_role": _MappingEntry(
        aws_equivalent="aws_iam_policy",
        score_pct=45,
        reason="Custom role permissions → IAM policy document.",
        notes=("GCP permissions don't map 1:1 to AWS actions; manual review required per role.",),
    ),
    "google_service_account_iam_binding": _MappingEntry(
        aws_equivalent="aws_iam_role_policy_attachment",
        score_pct=40,
        reason="Workload Identity binding → IRSA trust policy on EKS.",
        notes=("Requires aws_iam_openid_connect_provider for the EKS cluster + trust policy referencing it.",),
    ),
    "google_compute_global_network_endpoint_group": _MappingEntry(
        aws_equivalent=None,
        score_pct=40,
        reason="NEG has no direct AWS equivalent — depends on workload type.",
        notes=("Serverless NEG → ALB → Lambda or container.", "Internet NEG → external target group.",),
    ),

    # VPN — multi-resource translation (gateway + customer gateway + connection).
    # Today MANUAL_REVIEW with high score (well-understood pattern, operator
    # picks BGP ASN + remote endpoints). Future: full translator landing
    # as part of the cross-module wiring sprint.
    "google_compute_vpn_gateway": _MappingEntry(
        aws_equivalent="aws_vpn_gateway",
        score_pct=70,
        reason="HA VPN gateway → AWS VPN Gateway. Tunnel topology + BGP settings translate; on-prem peer IPs come from operator.",
        notes=(
            "Maps to aws_vpn_gateway + aws_customer_gateway (per peer) + aws_vpn_connection (per tunnel).",
            "BGP ASN, peer IPs, and pre-shared keys are operator-supplied per customer landing zone.",
        ),
    ),
    "google_compute_vpn_tunnel": _MappingEntry(
        aws_equivalent="aws_vpn_connection",
        score_pct=70,
        reason="Per-tunnel translation under a single aws_vpn_gateway.",
        notes=(
            "Each GCP tunnel becomes one aws_vpn_connection with its tunnel-specific PSK + peer IP.",
            "GCP IPsec defaults map cleanly; only BGP routing config needs operator review.",
        ),
    ),

    # BigQuery — scaffold translator (Kiro-review fix #8, 2026-05-12).
    # Bumped from MANUAL_REVIEW to LOW because we now emit an Athena
    # workgroup scaffold; data migration remains operator-driven.
    "google_bigquery_dataset": _MappingEntry(
        aws_equivalent="aws_athena_workgroup",
        score_pct=55,
        reason="BigQuery dataset → Athena workgroup + Glue catalog DB (default) or Redshift Serverless (commented alternative in module).",
        notes=(
            "Athena is the default target for ad-hoc analytics over S3-backed data.",
            "Redshift Serverless is the alternative when BI tooling / high concurrency / complex joins are required.",
            "Data migration (BQ → GCS → S3 → Athena) is a separate workstream — see migration_helpers/.",
            "BigQuery views and stored procedures need operator rewrite as Athena CREATE VIEW or Redshift materialized views.",
        ),
    ),
    "google_bigquery_table": _MappingEntry(
        aws_equivalent="aws_glue_catalog_table",
        score_pct=50,
        reason="BigQuery table → Glue catalog table pointing at S3-backed Parquet data.",
        notes=(
            "Table schema translates via Glue crawler or explicit CREATE EXTERNAL TABLE in Athena.",
            "Partitioning preserved if data is exported with the partition columns as folder structure.",
        ),
    ),

    # -----------------------------------------------------------------
    # Coverage expansion 2026-05-12: unmapped types surfaced by audit
    # of `unknown_*` synthetic types on the DH customer fixture. Each
    # entry below replaces a fall-through "Add this type to coverage.py"
    # error with a real AWS mapping or a curated MANUAL_REVIEW reason.
    # -----------------------------------------------------------------

    # CI/CD — Cloud Build → AWS CodeBuild / CodePipeline / CodeStar
    "google_cloudbuild_trigger": _MappingEntry(
        aws_equivalent="aws_codepipeline",
        score_pct=65,
        reason="Cloud Build trigger → CodePipeline (preferred when multi-stage) or EventBridge → CodeBuild.",
        notes=(
            "Triggers on push/PR translate via CodeStar Connection → CodePipeline source stage.",
            "Build steps map to a CodeBuild action; operator copies the buildspec from Cloud Build's YAML.",
        ),
    ),
    "google_cloudbuild_worker_pool": _MappingEntry(
        aws_equivalent="aws_codebuild_project",
        score_pct=60,
        reason="Cloud Build private worker pool → CodeBuild project with VPC config (private build env).",
        notes=(
            "Worker pool's VPC + subnet config → CodeBuild project's vpc_config block.",
            "Compute machine type (e1-standard-N) maps to CodeBuild compute_type (BUILD_GENERAL1_*).",
        ),
    ),
    "google_cloudbuildv2_repository": _MappingEntry(
        aws_equivalent="aws_codestarconnections_connection",
        score_pct=60,
        reason="Cloud Build v2 Repository (GitHub/GitLab/Bitbucket) → CodeStar Connections.",
        notes=(
            "Establishes the OAuth/PAT connection AWS CodePipeline uses to clone source repos.",
            "Authentication setup is a one-time manual step in the AWS console after the resource exists.",
        ),
    ),

    # Async work / queues
    "google_cloud_tasks_queue": _MappingEntry(
        aws_equivalent="aws_sqs_queue",
        score_pct=70,
        reason="Cloud Tasks queue → SQS standard queue. Rate-limit + retry config map closely.",
        notes=(
            "GCP Cloud Tasks pushes to HTTP endpoints; AWS SQS is pull-only — front with Lambda or ECS poller.",
            "App-Engine target type has no AWS analog — redeploy worker as Lambda.",
        ),
    ),

    # AlloyDB → Aurora PostgreSQL
    "google_alloydb_instance": _MappingEntry(
        aws_equivalent="aws_rds_cluster",
        score_pct=75,
        reason="AlloyDB (HA Postgres) → Aurora PostgreSQL cluster. Engine + storage tier align closely.",
        notes=(
            "AlloyDB's columnar engine has no direct AWS equivalent — use Aurora's parallel query for similar OLAP workloads.",
            "Read-replica topology translates 1:1; backup retention + PITR config differ slightly.",
        ),
    ),
    "google_alloydb_cluster": _MappingEntry(
        aws_equivalent="aws_rds_cluster",
        score_pct=75,
        reason="AlloyDB cluster → Aurora cluster. Multi-AZ + writer/reader topology preserved.",
        notes=("Cluster-level password + maintenance window translate; backup policy differs.",),
    ),

    # Project & Org hierarchy (no direct 1:1 — operator architectural decisions)
    "google_project": _MappingEntry(
        aws_equivalent="aws_organizations_account",
        score_pct=40,
        reason="GCP Project → AWS Account. Conceptual analog only; account-creation is governance-gated.",
        notes=(
            "AWS Organizations accounts are typically pre-provisioned via Control Tower / IT processes — not Terraform-managed in app repos.",
            "Most GCP-Project resources translate at the per-resource level (IAM, billing tags) rather than account-level.",
        ),
    ),
    "google_folder_iam_binding": _MappingEntry(
        aws_equivalent="aws_organizations_policy_attachment",
        score_pct=55,
        reason="Folder-level IAM → OU-attached SCP. Hierarchical access control concept maps.",
        notes=(
            "GCP folder IAM grants ROLES; AWS SCPs DENY actions (policy model is inverted).",
            "Operator rewrites principal+role pairs as SCP allow/deny statements.",
        ),
    ),
    "google_folder_iam_member": _MappingEntry(
        aws_equivalent="aws_organizations_policy_attachment",
        score_pct=55,
        reason="Folder-level IAM member binding — see google_folder_iam_binding.",
        notes=(),
    ),
    "google_org_policy_policy": _MappingEntry(
        aws_equivalent="aws_organizations_policy",
        score_pct=60,
        reason="Org Policy constraint → AWS Service Control Policy (SCP).",
        notes=(
            "GCP Org Policy uses constraint codes (e.g., constraints/iam.disableServiceAccountKeyCreation); AWS SCPs use JSON action/resource statements.",
            "Operator translates each constraint by hand — there's no automated rule.",
        ),
    ),
    "google_tags_tag_value": _MappingEntry(
        aws_equivalent="aws_resourcegroups_group",
        score_pct=50,
        reason="GCP resource tag → AWS resource tag (key/value pairs on most AWS resources).",
        notes=(
            "AWS doesn't have a separate 'tag value' resource — tags are inline attributes on each resource.",
            "Migrator emits the tag values as a Resource Groups definition operators can reference from tagged-resource queries.",
        ),
    ),
    "google_tags_tag_binding": _MappingEntry(
        aws_equivalent="aws_resourcegroupstaggingapi_tagging",
        score_pct=50,
        reason="Tag binding (resource ↔ tag) → AWS resource-tag application via tagging API.",
        notes=("Customer's downstream resources typically pick up tags via provider default_tags block.",),
    ),
    "google_project_service": _MappingEntry(
        aws_equivalent=None,
        score_pct=10,
        reason="GCP Project Service (API enablement) — AWS has no equivalent; services are always enabled.",
        notes=(
            "GCP requires explicit API enablement (`compute.googleapis.com`) before resources can be created.",
            "AWS services are universally available — no opt-in resource needed. Safe to drop these blocks.",
        ),
    ),
}


# Resources Kiro flags as MANUAL_REVIEW because no AWS equivalent
# exists at all (or the paradigm shift is so large that translation
# guidance must be operator-driven).
_MANUAL_REVIEW_TYPES = {
    "google_apigee_organization":  "Apigee has no direct AWS equivalent. Consider API Gateway or Kong on EKS — paradigm shift requires architectural decision.",
    "google_apigee_environment":   "Apigee environment — see google_apigee_organization note.",
    "google_apigee_instance":      "Apigee instance — see google_apigee_organization note.",
    "google_firestore_database":   "Firestore RTDB triggers → DynamoDB Streams + Lambda, but Firebase auth model needs rewiring.",
    "google_cloudfunctions2_function": "Cloud Functions v2 with Firebase RTDB triggers requires application-level changes; not directly translatable.",
    "google_dataflow_job":          "Dataflow streaming pipelines → Kinesis Data Firehose + Glue or AWS MWAA; DAG/template rewrite required.",
    "google_composer_environment":  "Cloud Composer (Airflow) → Amazon MWAA. DAG code is portable but environment config differs significantly.",
    "google_dataform_repository":   "Dataform has no direct AWS equivalent — consider Glue or dbt on MWAA.",
    "google_filestore_instance":    "Filestore → Amazon EFS. NFS mount points and IAM model differ.",
    "google_cloud_run_v2_service":  "Cloud Run v2 → ECS Fargate or AWS App Runner. Container config translates; networking and auth need review.",
    # BigQuery removed from MANUAL_REVIEW — moved to _GCP_TO_AWS as LOW
    # (Athena scaffold translator landed 2026-05-12, Kiro-review fix #8).
    "google_vpc_access_connector":  "Serverless VPC Connector → AWS PrivateLink or VPC endpoint. GCP-specific construct.",

    # -----------------------------------------------------------------
    # Coverage expansion 2026-05-12 — 3rd-party + niche types surfaced
    # by audit of `unknown_*` synthetic types. The inventory walker now
    # classifies these instead of leaving them as fall-through unknowns,
    # but they don't have direct AWS equivalents.
    # -----------------------------------------------------------------

    # Anthos Service Mesh — GCP-specific. AWS App Mesh is conceptually
    # similar but uses Envoy-on-EC2/ECS, not in-cluster GKE-Hub features.
    "google_gke_hub_feature":
        "Anthos Service Mesh (GKE Hub feature) → AWS App Mesh or Istio-on-EKS. ASM-specific traffic policies and mTLS config need full rewrite.",

    # Service-usage quota overrides — niche, rarely needs translation.
    "google_service_usage_consumer_quota_override_v1beta":
        "Service quota override — AWS uses `aws_servicequotas_service_quota` per quota; one-off operator decision per quota.",

    # Cross-stack data sources (GCP-specific `data "google_project"` pattern)
    "google_project_data_source":
        "Cross-project data source for reading project_number / project_id from another project. AWS equivalent: `data \"aws_caller_identity\"` or `data \"aws_organizations_organization\"`. Often unnecessary in AWS — resources don't reference parent account by number.",

    # SQL import jobs (one-off data loads against an existing DB)
    "google_sql_import_job":
        "One-off Cloud SQL data import (not a database resource). AWS equivalent: AWS DMS migration task OR psql/mysql client invocation post-cluster-create. Not auto-translatable — operator runs the import after the target DB exists.",

    # 3rd-party providers — not GCP or AWS resources at all
    "auth0_provider":
        "Auth0 SaaS — keep using the Auth0 provider in AWS as well, OR migrate to AWS Cognito (architectural decision; user pools + identity pools rewrite).",
    "octopusdeploy_resource":
        "Octopus Deploy — 3rd-party CD platform. Either keep Octopus pointing at AWS resources, or migrate to AWS CodeDeploy / CodePipeline.",
    "googleworkspace_drive_folder":
        "Google Drive folder (Workspace provider) — no AWS equivalent. Keep using Google Workspace OR migrate file shares to S3 + IAM-controlled access.",
}


def map_gcp_to_aws(tf_type: str) -> _MappingEntry:
    """Look up the AWS mapping for a GCP resource type.

    Returns a synthetic MANUAL_REVIEW entry for unknown types so
    callers don't need to special-case absence.

    Two unmapped cases, two different fix paths:
      1. ``unknown_<segment>`` — the inventory walker couldn't infer
         the GCP type from the Terragrunt module path. Fix in
         ``migrator/ingest/inventory.py:_INFER_RULES`` by adding a
         substring pattern that maps to the real ``google_*`` type.
      2. ``google_<x>`` we just haven't categorised — Fix in
         ``migrator/plan/coverage.py:_GCP_TO_AWS`` by adding a mapping.
    Both messages now point at the right file so operators don't waste
    time editing the wrong layer.
    """
    if tf_type in _GCP_TO_AWS:
        return _GCP_TO_AWS[tf_type]
    if tf_type in _MANUAL_REVIEW_TYPES:
        return _MappingEntry(
            aws_equivalent=None,
            score_pct=0,
            reason=_MANUAL_REVIEW_TYPES[tf_type],
            notes=("Flagged as MANUAL_REVIEW — operator must decide architecture.",),
        )
    # `unknown_*` is the synthetic prefix from ``infer_gcp_type_from_module_path``
    # when no inference rule matched. Different fix path than a real
    # ``google_*`` type we just haven't added — point operator at the
    # correct file.
    if tf_type.startswith("unknown_"):
        segment = tf_type[len("unknown_"):]
        return _MappingEntry(
            aws_equivalent=None,
            score_pct=0,
            reason=(
                f"Inventory walker couldn't classify Terragrunt stack matching "
                f"`{segment}` — synthetic placeholder `{tf_type}` emitted."
            ),
            notes=(
                "Fix in migrator/ingest/inventory.py:_INFER_RULES — add a substring "
                f"pattern (e.g., `\"{segment.replace('_', '-')}\"`) that maps to the "
                "correct `google_*` type. Then re-run; the proper mapping will "
                "take over from coverage.py's existing table.",
                "If the stack is genuinely external (Auth0, Octopus, etc.) and "
                "has no GCP/AWS analog, add it to _MANUAL_REVIEW_TYPES instead.",
            ),
        )
    # Real `google_*` type we haven't mapped yet.
    return _MappingEntry(
        aws_equivalent=None,
        score_pct=0,
        reason=f"No mapping rule for `{tf_type}` in our coverage table.",
        notes=(
            "Add this type to migrator/plan/coverage.py:_GCP_TO_AWS to enable "
            "translation, OR to _MANUAL_REVIEW_TYPES if it has no clean AWS "
            "equivalent (with a one-line reason for the operator).",
        ),
    )


def score_resources(
    resources: List[DiscoveredResource],
    *,
    target_cloud: str,
) -> List[ConfidenceFinding]:
    """Score every resource using the mapping table above.

    Currently target_cloud is always ``aws``; the parameter exists so
    the signature stays stable when Azure / OCI mapping tables land.
    """
    if target_cloud.lower() != "aws":
        # Defensive — caller's preflight should already enforce this.
        return []

    findings: List[ConfidenceFinding] = []
    for r in resources:
        entry = map_gcp_to_aws(r.tf_type)
        findings.append(ConfidenceFinding(
            resource_address=r.address,
            tf_type=r.tf_type,
            band=_band_for(entry.score_pct),
            score_pct=entry.score_pct,
            aws_equivalent=entry.aws_equivalent,
            reason=entry.reason,
            notes=list(entry.notes),
        ))

    # Stable order: by tf_type then name (matches the inventory order).
    findings.sort(key=lambda c: c.resource_address)
    return findings
