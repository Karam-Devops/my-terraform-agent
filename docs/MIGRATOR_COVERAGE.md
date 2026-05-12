# Cloud Lifecycle Intelligence — Migrator Coverage

_Generated: `2026-05-12T07:06+00:00` from `migrator/plan/coverage.py` + `migrator/translate/__init__.py`._

This document is the **canonical answer** to *"What GCP resources can the Migrator engine translate to AWS today?"* It's machine-generated from the engine's mapping table, so changes here reflect actual engine behavior — not aspirations.

## Summary

| Band | Count | Meaning |
|---|---|---|
| 🟢 **HIGH** (≥85%) | 19 | Translates with minimal review |
| 🟡 **MEDIUM** (60–84%) | 20 | Engineer pass per resource (topology shifts: SG/IAM model, etc.) |
| 🔴 **LOW** (<60%) | 7 | Paradigm shifts (IAM bindings, IRSA wiring) — careful design |
| ⚠️ **MANUAL_REVIEW** | 0 | No direct AWS equivalent or customer-specific module |
| **Total** | **46** | |

**Translators registered today: 23** of 46 mappable types (50%).

✅ = translator registered, emits AWS module body. ⏳ = mapping known, translator pending. 🚫 = no AWS equivalent.

## 🟢 HIGH confidence — 19 resource types

| Status | GCP type | AWS equivalent | Score | Reason |
|---|---|---|---|---|
| ✅ | `google_artifact_registry_repository` | `aws_ecr_repository` | 88% | Docker / Helm registries map to ECR repos. |
| ✅ | `google_compute_address` | `aws_eip` | 88% | Static IP allocation + region map directly to Elastic IP. |
| ✅ | `google_compute_firewall` | `aws_security_group` | 87% | Allow rules translate to SG ingress; deny rules need NACL. |
| ✅ | `google_compute_network` | `aws_vpc` | 90% | VPC topology translates directly; CIDR ranges + flow logs preserved. |
| ⏳ | `google_compute_router` | `aws_internet_gateway` | 85% | Cloud Router's BGP role partly fills an AWS Internet Gateway role for egress. |
| ✅ | `google_compute_router_nat` | `aws_nat_gateway` | 88% | Outbound NAT translates 1:1. |
| ✅ | `google_compute_subnetwork` | `aws_subnet` | 90% | Subnet CIDR + secondary ranges + private Google access map cleanly. |
| ✅ | `google_dns_managed_zone` | `aws_route53_zone` | 85% | Public/private managed zone maps directly to Route53. |
| ⏳ | `google_dns_record_set` | `aws_route53_record` | 85% | Record sets translate; routing policy options differ. |
| ⏳ | `google_network_connectivity_hub` | `aws_ec2_transit_gateway` | 87% | Hub topology translates to TGW; spoke VPCs become TGW attachments. |
| ⏳ | `google_network_connectivity_spoke` | `aws_ec2_transit_gateway_vpc_attachment` | 85% | Spoke VPC + linked services map to TGW VPC attachment. |
| ✅ | `google_pubsub_subscription` | `aws_sqs_queue` | 85% | Subscription ack deadline + retention + retry policy map to SQS queue config. |
| ✅ | `google_pubsub_topic` | `aws_sns_topic` | 85% | Topic + retention + storage policy translate; subscriptions become SNS→SQS fan-out. |
| ✅ | `google_redis_instance` | `aws_elasticache_replication_group` | 88% | Memorystore Redis configuration maps to ElastiCache Redis cluster. |
| ✅ | `google_secret_manager_secret` | `aws_secretsmanager_secret` | 85% | Secret name + replication policy map directly. |
| ⏳ | `google_sql_database` | `aws_db_instance` | 85% | Database creation rolled into the parent RDS instance config. |
| ✅ | `google_sql_database_instance` | `aws_db_instance` | 92% | PostgreSQL/MySQL versions, tier, disk size, backup config translate cleanly. |
| ✅ | `google_storage_bucket` | `aws_s3_bucket` | 90% | Storage class, versioning, lifecycle rules, CORS map directly. |
| ⏳ | `google_storage_bucket_iam_member` | `aws_s3_bucket_policy` | 85% | Per-bucket IAM grant translates to bucket policy statement. |

### Notes / caveats

**`google_artifact_registry_repository`**:
  - Cross-repo replication policies need separate AWS ECR replication config.

**`google_compute_firewall`**:
  - GCP firewall is VPC-attached; AWS SG is instance-attached — topology shift.
  - Egress-default in GCP differs from AWS (AWS SG default-allow-egress).

**`google_compute_network`**:
  - Routing mode (REGIONAL/GLOBAL) has no direct AWS analog — defaults to single-region.

**`google_compute_router`**:
  - Cloud Router supports BGP for hybrid; AWS equivalent is Direct Connect Gateway / TGW.

**`google_compute_router_nat`**:
  - AWS NAT GW is per-AZ; GCP Cloud NAT is per-VPC — emit one NAT GW per AZ for HA.

**`google_compute_subnetwork`**:
  - AWS subnets are zonal; GCP subnets are regional — one GCP subnet may emit one AWS subnet per AZ.

**`google_network_connectivity_hub`**:
  - STAR topology may need TGW route tables to enforce.

**`google_pubsub_subscription`**:
  - Push subscriptions translate to SNS HTTP/Lambda subscriptions.

**`google_pubsub_topic`**:
  - GCP single-resource topic+sub model splits into AWS SNS topic + SQS queue per subscription.

**`google_secret_manager_secret`**:
  - Secret versions migrate via the migration helper script.

**`google_sql_database`**:
  - Multiple GCP databases per instance → multiple AWS RDS databases (CREATE DATABASE in user_data).

**`google_sql_database_instance`**:
  - Cloud SQL HA (REGIONAL) → RDS Multi-AZ (different replication semantics).
  - PSA private IP requires AWS RDS in the same VPC, no equivalent peering needed.

**`google_storage_bucket`**:
  - Uniform bucket-level access maps to S3 bucket policy + Block Public Access.

**`google_storage_bucket_iam_member`**:
  - Multiple GCP iam_members on same bucket collapse to single AWS policy doc.

## 🟡 MEDIUM confidence — 20 resource types

| Status | GCP type | AWS equivalent | Score | Reason |
|---|---|---|---|---|
| ✅ | `google_certificate_manager_certificate` | `aws_acm_certificate` | 70% | Certificate Manager translates to ACM. |
| ✅ | `google_cloud_scheduler_job` | `aws_scheduler_schedule` | 65% | Cron jobs map to EventBridge Scheduler. |
| ✅ | `google_compute_backend_service` | `aws_lb_target_group` | 75% | Backend service → target group; health checks translate. |
| ⏳ | `google_compute_disk` | `aws_ebs_volume` | 82% | Disk type + size + zone map to EBS. |
| ✅ | `google_compute_global_address` | `aws_globalaccelerator_accelerator` | 65% | Global anycast IPs partially translate — VPC peering use needs special handling. |
| ✅ | `google_compute_global_forwarding_rule` | `aws_lb` | 72% | External HTTPS LB → ALB. CDN-fronted requires CloudFront. |
| ⏳ | `google_compute_health_check` | `aws_lb_target_group` | 78% | Health-check parameters fold into ALB target group health_check block. |
| ✅ | `google_compute_instance` | `aws_instance` | 75% | Machine type, boot disk, network interface translate. |
| ✅ | `google_compute_security_policy` | `aws_wafv2_web_acl` | 72% | Cloud Armor policy → AWS WAF v2 ACL. Rate limiting + geo-blocking translate. |
| ⏳ | `google_compute_ssl_certificate` | `aws_acm_certificate` | 70% | Managed cert translates to ACM; manual upload also supported. |
| ⏳ | `google_compute_ssl_policy` | `aws_lb_listener` | 68% | SSL policy folds into ALB listener `ssl_policy` attribute. |
| ✅ | `google_container_cluster` | `aws_eks_cluster` | 78% | Cluster networking + release channel translate; Workload Identity → IRSA needs SA email rewiring. |
| ✅ | `google_container_node_pool` | `aws_eks_node_group` | 78% | Node config (machine type, autoscaling, disk) translates; taints map cleanly. |
| ⏳ | `google_kms_crypto_key` | `aws_kms_key` | 80% | Symmetric encryption key + rotation period translate. |
| ⏳ | `google_kms_key_ring` | _(none)_ | 70% | AWS KMS has no key-ring abstraction — collapses into the key. |
| ⏳ | `google_logging_project_bucket_config` | `aws_cloudwatch_log_group` | 68% | Log retention + analytics maps to CloudWatch log group + retention. |
| ✅ | `google_logging_project_sink` | `aws_kinesis_firehose_delivery_stream` | 65% | Log routing translates to CloudWatch Logs subscription + Kinesis Firehose. |
| ⏳ | `google_monitoring_alert_policy` | `aws_cloudwatch_metric_alarm` | 65% | Alert conditions translate; some metric paths differ. |
| ⏳ | `google_monitoring_notification_channel` | `aws_sns_topic` | 70% | Notification channel → SNS topic with email subscription. |
| ⏳ | `google_monitoring_uptime_check_config` | `aws_route53_health_check` | 72% | Uptime check translates to Route53 health check. |

### Notes / caveats

**`google_compute_disk`**:
  - pd-balanced → gp3, pd-ssd → io2, pd-standard → gp2.

**`google_compute_global_address`**:
  - PSA reservations have no direct AWS equivalent — use VPC peering or PrivateLink.

**`google_compute_global_forwarding_rule`**:
  - NEG backends have no direct AWS equivalent — use TG with EKS service or ECS service.

**`google_compute_instance`**:
  - Service account (GCP) → instance profile (AWS) — different attachment model.
  - Metadata (enable-oslogin, ssh-keys) → user_data + SSM.
  - OS Login has no direct AWS equivalent — use SSM Session Manager.

**`google_compute_security_policy`**:
  - Custom CEL rules need rewriting as AWS WAF JSON statements.

**`google_compute_ssl_certificate`**:
  - Cert validation method differs (DNS validation strongly preferred in ACM).

**`google_container_cluster`**:
  - master_ipv4_cidr_block has no direct EKS equivalent.
  - Private cluster + master_authorized_networks → EKS endpoint config + SG.
  - GKE Autopilot has no direct EKS equivalent — use EKS Fargate.

**`google_container_node_pool`**:
  - Preemptible nodes → EC2 Spot via launch-template capacity_type=SPOT.
  - Workload metadata (GKE_METADATA) → IRSA (no direct equivalent setting).

**`google_kms_crypto_key`**:
  - GCP keyring + key (2 resources) → AWS KMS key (1 resource).
  - Key policy replaces resource IAM bindings (different model).

**`google_kms_key_ring`**:
  - Key ring becomes a tagging/naming convention on aws_kms_key.

**`google_logging_project_sink`**:
  - Filter expressions need rewriting (LQL → CloudWatch Logs Insights or filter pattern).

**`google_monitoring_alert_policy`**:
  - MQL queries don't translate — needs rewrite to CloudWatch metric math.

**`google_monitoring_uptime_check_config`**:
  - Or use CloudWatch Synthetics for richer behavioral checks.

## 🔴 LOW confidence — 7 resource types

| Status | GCP type | AWS equivalent | Score | Reason |
|---|---|---|---|---|
| ⏳ | `google_compute_global_network_endpoint_group` | _(none)_ | 40% | NEG has no direct AWS equivalent — depends on workload type. |
| ⏳ | `google_project_iam_binding` | `aws_iam_policy` | 45% | Project IAM binding → IAM policy with role assumption statement. |
| ⏳ | `google_project_iam_custom_role` | `aws_iam_policy` | 45% | Custom role permissions → IAM policy document. |
| ⏳ | `google_project_iam_member` | `aws_iam_role_policy_attachment` | 45% | Resource-attached IAM binding → identity-attached policy. Topology shift. |
| ⏳ | `google_service_account` | `aws_iam_role` | 50% | Service account → IAM role. Workload Identity → IRSA needs full rewiring. |
| ⏳ | `google_service_account_iam_binding` | `aws_iam_role_policy_attachment` | 40% | Workload Identity binding → IRSA trust policy on EKS. |
| ⏳ | `google_service_networking_connection` | _(none)_ | 55% | GCP PSA peering has no AWS-native equivalent — RDS uses subnet groups directly. |

### Notes / caveats

**`google_compute_global_network_endpoint_group`**:
  - Serverless NEG → ALB → Lambda or container.
  - Internet NEG → external target group.

**`google_project_iam_binding`**:
  - Member-list semantics differ; AWS policies are document-based.

**`google_project_iam_custom_role`**:
  - GCP permissions don't map 1:1 to AWS actions; manual review required per role.

**`google_project_iam_member`**:
  - Granular AWS managed policies needed; not all GCP roles have direct equivalents.
  - Custom roles need separate translation step.

**`google_service_account`**:
  - GCP SAs are identities; AWS IAM roles are assumable. Different attachment model.
  - All `serviceAccount:...@...iam.gserviceaccount.com` member references need rewriting.

**`google_service_account_iam_binding`**:
  - Requires aws_iam_openid_connect_provider for the EKS cluster + trust policy referencing it.

**`google_service_networking_connection`**:
  - Replace with aws_db_subnet_group; managed-service VPC peering not needed in AWS.

---

## How to extend coverage

1. **Add a mapping entry** in `migrator/plan/coverage.py` `_GCP_TO_AWS` dict.
2. **Author a translator** at `migrator/translate/<service>.py` exporting `translate()` + `aws_module_spec()`.
3. **Register it** in `migrator/translate/__init__.py` `TRANSLATORS` dict.
4. **Re-run this generator** to update this doc:
   ```
   python -m migrator.plan.publish_mapping_table
   ```

See `migrator/translate/customer_profiles/README.md` for adding customer-specific local-ref substitutions without touching engine code.
