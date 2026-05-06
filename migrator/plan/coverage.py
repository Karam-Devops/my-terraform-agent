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
    "google_bigquery_dataset":      "BigQuery → Redshift Serverless or Athena+S3. Schema and partitioning logic need review; data migration is a separate workstream.",
    "google_bigquery_table":        "BigQuery table — see google_bigquery_dataset note.",
    "google_vpc_access_connector":  "Serverless VPC Connector → AWS PrivateLink or VPC endpoint. GCP-specific construct.",
}


def map_gcp_to_aws(tf_type: str) -> _MappingEntry:
    """Look up the AWS mapping for a GCP resource type.

    Returns a synthetic MANUAL_REVIEW entry for unknown types so
    callers don't need to special-case absence.
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
    return _MappingEntry(
        aws_equivalent=None,
        score_pct=0,
        reason=f"No mapping rule for `{tf_type}` in our table — operator must review.",
        notes=("Add this type to migrator/plan/coverage.py:_GCP_TO_AWS to enable translation.",),
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
