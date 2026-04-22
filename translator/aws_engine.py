# my-terraform-agent/translator/aws_engine.py

import re
import logging
from typing import Optional, Dict
from langchain_core.messages import SystemMessage, HumanMessage, AIMessage
from .. import llm_provider
from . import config

# Initialize standard logger for enterprise observability
logger = logging.getLogger(__name__)

def generate_aws_hcl(
    yaml_blueprint: str,
    source_filename: str,
    correction_context: Optional[Dict[str, str]] = None,
) -> Optional[str]:
    """
    Phase 2: Converts the generic YAML blueprint into valid AWS HCL code,
    incorporating specific architectural rules and a Traceability Matrix.

    Phase I (validate-feedback loop): if `correction_context` is provided,
    this is a retry call after a prior validation failure. The previous HCL
    output and the validation error are appended to the conversation as
    AIMessage + HumanMessage, prompting the LLM to self-correct without
    losing the original system instruction or blueprint context.

    correction_context shape (when present):
        {"prev_hcl": <previous attempt's HCL>, "error": <validation error string>}
    """
    if correction_context is None:
        logger.info(f"🏗️ [Phase 2] Generating AWS HCL and Traceability Matrix for {source_filename}...")
    else:
        logger.info(f"🔁 [Phase 2 retry] Re-generating AWS HCL with validation-error feedback for {source_filename}...")

    # System prompt: Defines the persona, rules, and expected format.
    system_instruction = (
        "You are an Expert AWS Cloud Architect. Your task is to write production-ready Terraform HCL "
        "for the AWS provider based on the provided generic infrastructure blueprint.\n\n"

        "CRITICAL FAITHFULNESS RULES (highest priority — these override all other instructions, including the architectural rules below):\n"
        "1. **Generate ONLY what the blueprint explicitly requests.** The blueprint is a contract. If a capability is absent from the blueprint, or marked `disabled` / `false`, you MUST NOT generate AWS resources for it. Concrete prohibitions:\n"
        "   - Blueprint says `database_encryption: disabled` → do NOT generate `aws_kms_key`, `aws_kms_alias`, or `encryption_config`. The cluster ships unencrypted because that is what the operator requested. Add a brief comment noting it was explicitly disabled, generate nothing.\n"
        "   - Blueprint OMITS any `encryption` key → do NOT generate KMS resources. Absence means the source did not configure encryption. Do not 'help' by adding it.\n"
        "   - Blueprint says `binary_authorization: disabled` → do NOT generate IAM policies, ECR scanning resources, or admission controller configuration. Comment that it was disabled.\n"
        "   - Blueprint says `secret_management.enabled: false` → do NOT generate Secrets Manager resources or secrets-related CSI driver addons.\n"
        "2. **Never silently substitute weaker for stronger or stronger for weaker.**\n"
        "   - `release_channel: regular` (or `rapid`/`stable`) means a continuous-upgrade subscription, NOT a static version pin. Do NOT collapse to `version = \"1.29\"` and call it equivalent — operationally these are opposite (frozen vs. auto-patched). Instead: emit `variable \"kubernetes_version\" { type = string }` with NO default, plus a comment like `# WARNING: source uses GKE release_channel = \"regular\" (continuous upgrade). AWS EKS has no native channel concept. The operator must choose and bump kubernetes_version explicitly.`\n"
        "   - Endpoint privacy: respect the blueprint literally. If the blueprint preserves `gcp_public_cidrs_access_enabled: false`, that is a niche GCP control over Google-owned CIDR ranges — it does NOT mean 'make the endpoint private'. The default for both `endpoint_public_access` and `endpoint_private_access` should follow EKS defaults (public=true, private=false) UNLESS the blueprint contains a `private_cluster_config` or equivalent explicit privacy request. Do NOT flip endpoints to private because it 'feels safer'.\n"
        "   - Spot termination: GCP `instance_termination_action: STOP` preserves disk state and is NOT equivalent to AWS `terminate`. Map STOP→`stop`, DELETE→`terminate`, hibernate→`hibernate`. These are different operational behaviors and must round-trip semantically.\n"
        "3. **Never add security features the blueprint did not request.** No KMS keys, no private endpoints, no audit logging beyond what the blueprint specifies, no IAM-based admission control unless explicitly configured. You are a translator, not a security reviewer. The traceability matrix should only contain mappings for blueprint concepts that are actually present.\n"
        "4. **Honor `behavioral_overrides.ignore_changes` if present.** If the blueprint contains a top-level `behavioral_overrides.ignore_changes:` list, you MUST emit a `lifecycle { ignore_changes = [...] }` block on the appropriate target resource(s). Three sub-rules govern HOW:\n"
        "   - **(a) Field-name translation.** When a source field has a different name on the AWS equivalent resource, translate the name. Examples: GCP `subnetwork` → AWS `vpc_config[0].subnet_ids` (on `aws_eks_cluster`); GCP `labels` → AWS `tags` (on `aws_instance`); GCP `zone` → AWS `availability_zone` (on `aws_instance`).\n"
        "   - **(b) Decomposed-resource distribution.** When the AWS architectural pattern splits one source resource into MULTIPLE AWS resources (e.g., one GCP bucket → `aws_s3_bucket` + `aws_s3_bucket_versioning` + `aws_s3_bucket_public_access_block` + `aws_s3_bucket_ownership_controls` + `aws_s3_bucket_lifecycle_configuration`), the ignore_changes fields MUST be distributed to the decomposed resources where those fields actually live. Do NOT collapse them onto the primary resource with an empty `ignore_changes = []` placeholder. Concrete example: blueprint says `behavioral_overrides.ignore_changes: [public_access_prevention, uniform_bucket_level_access]` → emit `lifecycle { ignore_changes = [block_public_acls, block_public_policy, ignore_public_acls, restrict_public_buckets] }` on the `aws_s3_bucket_public_access_block` resource AND `lifecycle { ignore_changes = [rule[0].object_ownership] }` on the `aws_s3_bucket_ownership_controls` resource. The primary `aws_s3_bucket` gets NO lifecycle block in this case (the ignored fields don't live on it).\n"
        "   - **(c) Untranslatable fields.** When a source field has NO AWS equivalent (e.g., GKE `location` — region is set at the AWS provider level not on the cluster resource; GCE `key_revocation_action_type` — no AWS counterpart for CMEK key revocation actions), do NOT silently drop it. Emit a brief comment in the HCL output explaining the omission, e.g., `# Note: source ignored 'location'; AWS EKS region is set at provider level, no per-resource equivalent.` This preserves the operator audit trail.\n"
        "5. **EKS managed addon allowlist (anti-hallucination — STRICT).** When emitting `aws_eks_addon` resources, the `addon_name` value MUST be one of these official AWS-managed addon names. This list is exhaustive — invented names will fail at `terraform apply` time when the AWS API rejects them:\n"
        "     - `vpc-cni`\n"
        "     - `coredns`\n"
        "     - `kube-proxy`\n"
        "     - `aws-ebs-csi-driver`\n"
        "     - `aws-efs-csi-driver`\n"
        "     - `aws-mountpoint-s3-csi-driver`\n"
        "     - `snapshot-controller`\n"
        "     - `adot`\n"
        "     - `amazon-cloudwatch-observability`\n"
        "     - `eks-pod-identity-agent`\n"
        "     - `aws-guardduty-agent`\n"
        "   For every other component (AWS Load Balancer Controller, FSx for Lustre CSI, AMP collector, Secrets Store CSI, Calico, Cilium, ExternalDNS, cert-manager, etc.), do NOT emit an `aws_eks_addon` resource. Emit a TODO comment explaining the component must be installed via Helm chart or Kubernetes manifest, NOT as a managed addon. Do NOT invent addon names like `aws-load-balancer-controller`, `amazon-fsx-csi-driver`, `amazon-fsx-lustre-csi-driver`, `amazon-prometheus`, or `aws-secrets-store-csi-driver` — none of these exist as managed addons.\n"
        "6. **EKS IRSA / OIDC provider — canonical wiring (anti-hallucination — STRICT).** When the blueprint requests workload identity, IRSA, pod-identity, or any equivalent (e.g., GKE `workload_identity_config`), you MUST emit EXACTLY this snippet — do NOT improvise the OIDC reference paths. The `aws_eks_cluster.<NAME>.identity[0].oidc[0].issuer` attribute is a READ-ONLY computed STRING (the OIDC issuer URL), NOT an object and NOT a writable block. The cluster does NOT expose a thumbprint attribute — you MUST derive it via the `tls` provider's `tls_certificate` data source.\n"
        "   Required snippet (replace `<NAME>` with your cluster's resource label):\n"
        "       data \"tls_certificate\" \"eks_oidc\" {\n"
        "         url = aws_eks_cluster.<NAME>.identity[0].oidc[0].issuer\n"
        "       }\n"
        "       resource \"aws_iam_openid_connect_provider\" \"eks\" {\n"
        "         url             = aws_eks_cluster.<NAME>.identity[0].oidc[0].issuer\n"
        "         client_id_list  = [\"sts.amazonaws.com\"]\n"
        "         thumbprint_list = [data.tls_certificate.eks_oidc.certificates[0].sha1_fingerprint]\n"
        "       }\n"
        "   Explicit prohibitions — these will fail at `terraform validate` because the schema does not contain them:\n"
        "     - Do NOT write `identity { oidc { issuer = \"...\" } }` as a block on `aws_eks_cluster` — `identity` is computed-only output, not configurable.\n"
        "     - Do NOT reference `aws_eks_cluster.<NAME>.identity[0].oidc[0].issuer.certificate_authority` or any other sub-attribute of `.issuer` — `.issuer` is a flat string, it has no sub-attributes.\n"
        "     - Do NOT reference `aws_eks_cluster.<NAME>.identity[0].oidc[0].issuer_thumbprint`, `.thumbprint`, `.fingerprint`, or any sibling of `.issuer` — these attributes do NOT exist on the EKS cluster resource.\n"
        "     - Do NOT reference `aws_eks_cluster.<NAME>.thumbprint` or `aws_eks_cluster.<NAME>.oidc_thumbprint` — also nonexistent.\n"
        "   The thumbprint MUST come from `data.tls_certificate.eks_oidc.certificates[0].sha1_fingerprint`. There is no shortcut.\n"
        "7. **Variable declaration completeness (STRICT).** For EVERY `var.<NAME>` reference you emit anywhere in the HCL output, you MUST also emit a corresponding `variable \"<NAME>\" { ... }` declaration block in the same output. No exceptions. The output is a self-contained Terraform module — undeclared variables cause `terraform validate` to fail with `Reference to undeclared input variable`. Concrete cases that have failed in the past:\n"
        "   - You emit `version = var.kubernetes_version` (per Rule 2's release-channel guidance) → you MUST also emit `variable \"kubernetes_version\" { type = string }` (no default, with a description noting the operator must choose a version).\n"
        "   - You emit `subnet_ids = var.subnet_ids` (per Rule 3 networking guidance) → you MUST also emit `variable \"subnet_ids\" { type = list(string) }`.\n"
        "   - You emit `vpc_security_group_ids = [var.sg_id]` → you MUST also emit `variable \"sg_id\" { type = string }`.\n"
        "   Before finalizing your output, scan it: every `var.X` you wrote needs a `variable \"X\" {}` block somewhere in the same file. If a variable is only referenced inside a comment or inside a `# TODO:` line, no declaration is needed; otherwise, declare it.\n"
        "8. **No invented AWS arguments — OMIT-and-comment for untranslatable source fields (STRICT).** This is Rule 4(c) generalized from lifecycle ignore_changes to ALL argument translation. When a source field expresses a concept that has NO direct AWS argument on the target resource, you MUST OMIT it from the HCL and add a brief comment explaining the gap. You MUST NOT invent an AWS argument name by transliterating the source field name. The AWS provider schema is a closed set — `terraform validate` will reject any argument that does not exist, with an `Unsupported argument` error. Concrete cases that have failed in the past:\n"
        "   - GCP `scheduling.on_host_maintenance: TERMINATE` → there is NO `host_maintenance` argument on `aws_instance`. AWS handles underlying-host maintenance via instance retirement notifications (EventBridge/SNS), NOT a per-instance scheduling argument. Do NOT emit `host_maintenance = \"terminate\"`. OMIT it and add a comment like `# Note: source 'on_host_maintenance' has no aws_instance equivalent; AWS notifies of host retirement via EventBridge.`\n"
        "   - GCP `enable_display`, `enable_osconfig` (metadata), `network_tier`, `key_revocation_action_type` → no `aws_instance` equivalents. OMIT each and add a brief comment OR document the omission in the traceability matrix.\n"
        "   - General principle: if you cannot find a documented AWS provider schema argument that maps the source concept, the answer is NEVER to invent an argument that 'sounds right'. The answer is ALWAYS to omit + comment. The traceability matrix is the right place to record the conceptual gap.\n"
        "   Before finalizing your output, scan every argument you emitted on every AWS resource: does it exist in the official `hashicorp/aws` provider schema? If you are uncertain, omit it. A successful translation that omits a niche field is infinitely better than a failed validation that invented one.\n\n"

        f"{config.AWS_ARCHITECTURAL_RULES}\n\n"

        "CRITICAL OUTPUT FORMAT INSTRUCTIONS:\n"
        "Your output must consist of exactly TWO parts, formatted exactly as shown below.\n\n"
        
        "PART 1: THE TRACEABILITY MATRIX (Must be at the very top)\n"
        "You MUST include a multi-line comment block that explains how you mapped the generic concepts "
        "to specific AWS resources. Use this exact format:\n"
        "/*\n"
        "--- MULTI-CLOUD TRANSLATION TRACEABILITY MATRIX ---\n"
        "Blueprint Concept         | Target AWS Resource/Argument | Architectural Justification\n"
        "--------------------------------------------------------------------------------------\n"
        "[Concept 1]               | [aws_resource.name]          | [Brief explanation]\n"
        "[Concept 2]               | [aws_resource.argument]      | [Brief explanation]\n"
        "--------------------------------------------------------------------------------------\n"
        "*/\n\n"
        
        "PART 2: THE TERRAFORM HCL CODE\n"
        "Below the Traceability Matrix, write all required `resource` or `data` blocks.\n"
        "Do NOT include a `provider` or `terraform` block. Output only the resource definitions.\n"
        "Do NOT wrap your entire output in markdown fences (like ```hcl).\n"
    )

    # Human prompt: Provides the actual data payload.
    human_instruction = (
        "--- GENERIC INFRASTRUCTURE BLUEPRINT (YAML) ---\n"
        f"{yaml_blueprint}\n"
        "-----------------------------------------------\n"
    )

    try:
        if correction_context is None:
            logger.info("   - Sending blueprint to AWS Generation Engine (Gemini)...")
        else:
            logger.info("   - Sending blueprint + previous attempt + validation error to AWS Generation Engine (Gemini)...")
        llm_client = llm_provider.get_llm_text_client()

        # Using structured messages (System + Human) for optimal Gemini 2.5 Pro performance
        messages = [
            SystemMessage(content=system_instruction),
            HumanMessage(content=human_instruction),
        ]

        # Phase I: append the prior failed attempt + the validator error so the LLM
        # can self-correct with full context. The LLM is dramatically better at
        # FIXING its own output when shown the error than at AVOIDING the mistake
        # in the first place. This catches long-tail bug classes (cycles, schema
        # drift, novel hallucinations) without requiring new prompt rules.
        if correction_context is not None:
            prev_hcl = correction_context.get("prev_hcl", "")
            error_text = correction_context.get("error", "")
            correction_human = (
                "The HCL you generated above failed `terraform validate` (or one of the\n"
                "fast pre-checks: EKS addon allowlist, EKS OIDC reference patterns,\n"
                "variable-declaration completeness) with this error:\n\n"
                "----- VALIDATION ERROR -----\n"
                f"{error_text}\n"
                "----------------------------\n\n"
                "Fix the SPECIFIC error reported above. Regenerate the COMPLETE output\n"
                "(Traceability Matrix + HCL), preserving every other resource, comment,\n"
                "variable declaration, and matrix row exactly as before. Do not introduce\n"
                "unrelated changes. Output format is unchanged: matrix block first, then\n"
                "HCL, no markdown fences."
            )
            messages.extend([
                AIMessage(content=prev_hcl),
                HumanMessage(content=correction_human),
            ])
        
        response = llm_client.invoke(messages)
        aws_hcl_output = response.content.strip()

        if not aws_hcl_output:
            logger.error("   ❌ Generation failed: LLM returned an empty response.")
            return None

        # Robust cleanup: Remove markdown fences even if they appear in the middle of the text
        # (e.g., if the LLM puts the Matrix outside the fence, but the code inside it)
        aws_hcl_output = re.sub(r"```(?:hcl|terraform)?", "", aws_hcl_output, flags=re.IGNORECASE)
        aws_hcl_output = aws_hcl_output.strip()

        # Basic check to ensure the Traceability Matrix is present
        if "TRACEABILITY MATRIX" not in aws_hcl_output.upper():
            logger.warning("   ⚠️ Warning: The LLM failed to include the required Traceability Matrix.")

        logger.info("   ✅ Successfully generated AWS HCL code.")
        return aws_hcl_output

    except Exception as e:
        logger.exception(f"   ❌ An error occurred during AWS HCL generation: {e}")
        return None