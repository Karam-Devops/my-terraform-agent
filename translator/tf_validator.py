# my-terraform-agent/translator/tf_validator.py

import os
import re
import tempfile
import subprocess
import logging
from typing import Tuple
from . import config

# Initialize standard logger for enterprise observability
logger = logging.getLogger(__name__)

# Authoritative list of AWS-managed EKS addon names.
# `terraform validate` does NOT catch invented addon names — addon validity is
# checked at `terraform apply` time when the AWS API rejects unknown values.
# This pre-check fails fast on the most common LLM hallucination class
# (e.g., aws-load-balancer-controller, amazon-fsx-csi-driver, amazon-prometheus)
# so the operator gets an actionable error message instead of a successful
# validate followed by an apply-time blowup.
# Source: AWS docs (List of EKS add-ons available from AWS).
# Update this list when AWS publishes new managed addons.
_EKS_MANAGED_ADDON_ALLOWLIST = frozenset({
    "vpc-cni",
    "coredns",
    "kube-proxy",
    "aws-ebs-csi-driver",
    "aws-efs-csi-driver",
    "aws-mountpoint-s3-csi-driver",
    "snapshot-controller",
    "adot",
    "amazon-cloudwatch-observability",
    "eks-pod-identity-agent",
    "aws-guardduty-agent",
})

# Matches `addon_name = "value"` with flexible whitespace. Captures the value.
_ADDON_NAME_RE = re.compile(r'addon_name\s*=\s*"([^"]+)"')

# OIDC / IRSA failure patterns. The `aws_eks_cluster.identity[0].oidc[0].issuer`
# attribute is a READ-ONLY computed STRING. Across multiple LLM runs it has been
# misused in three distinct shapes — each fails at terraform validate, but with
# verbose schema errors that don't immediately point at the root cause. We catch
# them with explicit, named patterns so the error message tells the operator
# (and the LLM, on retry) exactly what shape was wrong AND what the right shape
# looks like. Each tuple is (compiled_regex, human_description).
_OIDC_BAD_PATTERNS = [
    # Sibling-attribute hallucinations: .issuer_thumbprint, .thumbprint,
    # .fingerprint, .oidc_thumbprint — none exist on aws_eks_cluster.
    (
        re.compile(r'aws_eks_cluster\.[A-Za-z0-9_\-]+\.identity\[\d+\]\.oidc\[\d+\]\.(issuer_thumbprint|thumbprint|fingerprint)'),
        "Invented sibling attribute on .identity[].oidc[] (e.g., `issuer_thumbprint`)",
    ),
    (
        re.compile(r'aws_eks_cluster\.[A-Za-z0-9_\-]+\.(thumbprint|oidc_thumbprint|oidc_fingerprint)\b'),
        "Invented top-level attribute on aws_eks_cluster (e.g., `thumbprint`, `oidc_thumbprint`)",
    ),
    # Object-traversal hallucinations: treats .issuer (a flat string) as having
    # sub-attributes like .certificate_authority.data
    (
        re.compile(r'\.identity\[\d+\]\.oidc\[\d+\]\.issuer\.[A-Za-z_]'),
        "Treats `.issuer` as an object with sub-attributes (it is a flat string URL)",
    ),
    # Block-syntax hallucination: tries to WRITE identity { oidc { issuer = "..." } }
    # on aws_eks_cluster. The identity attribute is computed-only.
    (
        re.compile(r'identity\s*\{\s*oidc\s*\{[^}]*issuer\s*=', re.DOTALL),
        "Treats computed `identity { oidc { issuer = ... } }` as a writable block",
    ),
]

# Canonical IRSA snippet shown in error messages. Mirrors the snippet pinned
# into the aws_engine.py prompt — keep these in sync if either changes.
_OIDC_CANONICAL_SNIPPET = '''data "tls_certificate" "eks_oidc" {
  url = aws_eks_cluster.<NAME>.identity[0].oidc[0].issuer
}

resource "aws_iam_openid_connect_provider" "eks" {
  url             = aws_eks_cluster.<NAME>.identity[0].oidc[0].issuer
  client_id_list  = ["sts.amazonaws.com"]
  thumbprint_list = [data.tls_certificate.eks_oidc.certificates[0].sha1_fingerprint]
}'''


def _check_eks_oidc_patterns(hcl_content: str) -> Tuple[bool, str]:
    """
    Pre-validation defensive check: scan the HCL for known-bad shapes of the
    EKS OIDC issuer reference. Returns (True, "") if clean; (False, msg) if any
    bad pattern matches. Each match is reported with both the offending text
    and the canonical correct snippet so the operator gets an actionable error.
    """
    hits = []
    for pattern, description in _OIDC_BAD_PATTERNS:
        for match in pattern.finditer(hcl_content):
            hits.append((description, match.group(0)))

    if not hits:
        return True, ""

    # Deduplicate by (description, matched_text) preserving first-seen order.
    seen = set()
    unique_hits = []
    for desc, text in hits:
        key = (desc, text)
        if key not in seen:
            seen.add(key)
            unique_hits.append((desc, text))

    msg_lines = [
        "EKS OIDC reference pattern check failed.",
        "",
        "The following expressions reference attributes or block shapes that do",
        "NOT exist on `aws_eks_cluster`. `terraform validate` would reject them",
        "with a verbose schema error; this pre-check fails fast with the root cause:",
        "",
    ]
    for desc, text in unique_hits:
        msg_lines.append(f"  - {desc}")
        msg_lines.append(f"      offending text: {text}")
    msg_lines.extend([
        "",
        "The `aws_eks_cluster.<NAME>.identity[0].oidc[0].issuer` attribute is a",
        "READ-ONLY computed STRING (the OIDC issuer URL). It has no sub-attributes",
        "and no sibling thumbprint attribute. Derive the thumbprint via the `tls`",
        "provider's `tls_certificate` data source.",
        "",
        "Canonical IRSA wiring (replace <NAME> with your cluster's resource label):",
        "",
        _OIDC_CANONICAL_SNIPPET,
    ])
    return False, "\n".join(msg_lines)


# Variable-declaration completeness check. The translator emits self-contained
# HCL modules — every `var.<NAME>` reference must be backed by a `variable "<NAME>" {}`
# block in the same output. A missing declaration causes terraform validate to fail
# with "Reference to undeclared input variable", but it reports them ONE AT A TIME
# (one validate run = one undeclared var error). This pre-check enumerates ALL
# missing declarations in a single pass so the operator (and the LLM, on retry)
# sees the complete list, not a one-error-at-a-time peel.
#
# The reference regex is intentionally narrow: `var.<identifier>` with word-boundary
# stops on either side. We strip comments first to avoid flagging `var.X` inside
# `# TODO: configure var.X later` lines, which are not real references.

# Matches `variable "<name>" {` declaration headers. The name capture allows
# letters, digits, underscores, and hyphens (terraform's identifier rules).
_VAR_DECL_RE = re.compile(r'variable\s+"([A-Za-z_][A-Za-z0-9_\-]*)"\s*\{')

# Matches `var.<name>` references. We use a lookbehind-free pattern with word
# boundary on the left and identifier-stop on the right, so `var.subnet_ids`
# matches but `myvar.foo` does not.
_VAR_REF_RE = re.compile(r'\bvar\.([A-Za-z_][A-Za-z0-9_\-]*)')

# Strip both line comments (#... and //...) and block comments (/* ... */) before
# scanning for var references. We DON'T want to flag `var.X` mentioned inside a
# TODO comment as a real reference — only actual code references count.
# Block-comment regex uses non-greedy + DOTALL to span newlines.
_LINE_COMMENT_RE = re.compile(r'(#|//).*?$', re.MULTILINE)
_BLOCK_COMMENT_RE = re.compile(r'/\*.*?\*/', re.DOTALL)


def _strip_comments(hcl_content: str) -> str:
    """Remove HCL comments so `var.X` inside a comment is not flagged as a reference."""
    no_block = _BLOCK_COMMENT_RE.sub("", hcl_content)
    no_line = _LINE_COMMENT_RE.sub("", no_block)
    return no_line


def _check_variable_declarations(hcl_content: str) -> Tuple[bool, str]:
    """
    Pre-validation defensive check: every `var.<NAME>` reference in the HCL
    (excluding references inside comments) must be backed by a corresponding
    `variable "<NAME>" {}` declaration block. Returns (True, "") if complete;
    (False, error_message) listing every undeclared reference.
    """
    declared = set(_VAR_DECL_RE.findall(hcl_content))

    code_only = _strip_comments(hcl_content)
    referenced = set(_VAR_REF_RE.findall(code_only))

    undeclared = sorted(referenced - declared)
    if not undeclared:
        return True, ""

    msg_lines = [
        "Variable declaration completeness check failed.",
        "",
        "The following `var.<NAME>` references appear in the HCL but have no",
        "matching `variable \"<NAME>\" {}` declaration block. `terraform validate`",
        "would fail with \"Reference to undeclared input variable\" — this pre-check",
        "lists every missing declaration in one pass instead of one-at-a-time:",
        "",
    ]
    for name in undeclared:
        msg_lines.append(f"  - var.{name}    (add: variable \"{name}\" {{ type = ... }})")
    msg_lines.extend([
        "",
        "Fix: emit a `variable \"<NAME>\" { type = ... }` block for each, OR remove",
        "the `var.<NAME>` reference if it was emitted unintentionally.",
    ])
    return False, "\n".join(msg_lines)


def _check_eks_addon_names(hcl_content: str) -> Tuple[bool, str]:
    """
    Pre-validation defensive check: extract every `addon_name = "..."` value
    in the HCL and confirm each is a real AWS-managed EKS addon. Returns
    (True, "") if all pass; (False, error_message) if any are unknown.
    """
    found_names = _ADDON_NAME_RE.findall(hcl_content)
    if not found_names:
        return True, ""

    unknown = [n for n in found_names if n not in _EKS_MANAGED_ADDON_ALLOWLIST]
    if not unknown:
        return True, ""

    # Deduplicate while preserving first-seen order for stable error messages.
    seen = set()
    unique_unknown = [n for n in unknown if not (n in seen or seen.add(n))]

    msg_lines = [
        "EKS managed addon allowlist check failed.",
        "",
        f"The following addon_name value(s) are NOT real AWS-managed EKS addons:",
    ]
    for name in unique_unknown:
        msg_lines.append(f"  - \"{name}\"")
    msg_lines.extend([
        "",
        "These would be silently accepted by `terraform validate` but rejected",
        "by the AWS API at `terraform apply` time. Likely fix: replace the",
        "`aws_eks_addon` resource with a TODO comment explaining the component",
        "must be installed via Helm chart or Kubernetes manifest.",
        "",
        f"Valid addon names: {', '.join(sorted(_EKS_MANAGED_ADDON_ALLOWLIST))}",
    ])
    return False, "\n".join(msg_lines)


def validate_hcl(hcl_content: str, target_cloud: str) -> Tuple[bool, str]:
    """
    Validates HCL syntax for the specified target cloud provider by running 
    an isolated 'terraform init' and 'terraform validate'.
    
    Returns:
        Tuple[bool, str]: (is_valid, error_message_or_success)
    """
    logger.info(f"🔍 [Pillar 1 Proof] Validating {target_cloud.upper()} HCL Syntax and Schema...")

    target = target_cloud.lower()

    # Target-agnostic pre-check: variable-declaration completeness. terraform
    # validate would catch this too, but reports one undeclared var per run.
    # Our check enumerates ALL of them in one pass for a complete error message.
    logger.info("   - Running variable-declaration completeness pre-check...")
    vars_ok, vars_msg = _check_variable_declarations(hcl_content)
    if not vars_ok:
        logger.warning("   ❌ Variable check failed: undeclared `var.X` reference(s) detected.")
        return False, vars_msg

    # Defensive pre-check: reject hallucinated EKS addon names BEFORE paying the
    # cost of `terraform init` + `terraform validate`. This bug class is invisible
    # to terraform validate (addon names are checked at apply time by the AWS API),
    # so without this check, a successful validate is misleading.
    if target == "aws":
        logger.info("   - Running EKS managed addon allowlist pre-check...")
        addons_ok, addons_msg = _check_eks_addon_names(hcl_content)
        if not addons_ok:
            logger.warning("   ❌ Allowlist check failed: hallucinated EKS addon name(s) detected.")
            return False, addons_msg

        logger.info("   - Running EKS OIDC reference pattern pre-check...")
        oidc_ok, oidc_msg = _check_eks_oidc_patterns(hcl_content)
        if not oidc_ok:
            logger.warning("   ❌ OIDC pattern check failed: invalid EKS OIDC reference shape detected.")
            return False, oidc_msg

    if target == "aws":
        provider_block = """
terraform {
  required_providers {
    aws = { source = "hashicorp/aws", version = "~> 5.0" }
  }
}
provider "aws" { region = "us-east-1" }
"""
    elif target == "azure":
        provider_block = """
terraform {
  required_providers {
    azurerm = { source = "hashicorp/azurerm", version = "~> 3.0" }
  }
}
provider "azurerm" {
  features {}
  skip_provider_registration = true
}
"""
    else:
        return False, f"Unknown target cloud: {target_cloud}"

    # Combine the mock provider with the generated code
    full_content = provider_block + "\n" + hcl_content

    # Run in an ephemeral directory to prevent state pollution
    with tempfile.TemporaryDirectory() as temp_dir:
        file_path = os.path.join(temp_dir, "main.tf")
        
        try:
            with open(file_path, "w", encoding="utf-8") as f:
                f.write(full_content)
                
            logger.info(f"   - Initializing {target_cloud.upper()} provider schema...")
            init_cmd = [config.TERRAFORM_PATH, "init", "-backend=false"]
            
            # Run init (throws CalledProcessError if it fails)
            subprocess.run(init_cmd, cwd=temp_dir, check=True, capture_output=True, text=True)
            
            logger.info("   - Running strict syntax and schema validation...")
            val_cmd = [config.TERRAFORM_PATH, "validate", "-no-color"]
            
            # Run validate (does not throw on failure, we check returncode manually)
            val_process = subprocess.run(val_cmd, cwd=temp_dir, capture_output=True, text=True)
            
            if val_process.returncode == 0:
                logger.info("   ✅ Validation Successful: The generated code is syntactically perfect.")
                return True, "Success"
            else:
                logger.warning("   ❌ Validation Failed: The LLM generated invalid schema/syntax.")
                # Return the stderr/stdout so it can be fed back to LangGraph/LLM for self-correction
                error_output = val_process.stderr.strip() or val_process.stdout.strip()
                return False, error_output

        except subprocess.CalledProcessError as e:
            # Captures failures during `terraform init` (e.g., network issues, bad provider block)
            logger.error(f"   ❌ Critical error running Terraform Init: {e.stderr}")
            return False, f"Terraform Init Failed: {e.stderr}"
        except Exception as e:
            # Captures unexpected OS/Python errors
            logger.exception(f"   ❌ Unexpected System Error during validation: {e}")
            return False, str(e)