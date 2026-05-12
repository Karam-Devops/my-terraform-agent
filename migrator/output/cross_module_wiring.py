"""Auto-wire cross-module references at emit time.

Closes the biggest gap surfaced by the Kiro Power code review: every
emitted env's main.tf has `vpc_id = "TODO-vpc-id"` etc. that the
operator has to manually fix before `terraform plan`. By inspecting
the set of modules being emitted in each env, we can replace many of
those TODOs with actual `module.X.Y` references.

Approach:
  1. Per-env: scan the list of (block_name, service_name) pairs being
     emitted in this env's main.tf.
  2. Apply the wiring table — for each known input-name → output-spec
     pattern, if both sides of the dependency are in this env, replace
     the TODO with the cross-module reference.
  3. Leave TODOs in place for inputs we can't resolve (no provider
     module in this env). Operator handles those manually.

Idempotent: running rewrite_inputs() on already-wired content is a
no-op (the regex only matches the literal `"TODO-X"` placeholders).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class WiringRule:
    """One wiring rule: when this input name appears in a module call,
    look for a provider module in the same env and rewrite the value
    to a module.X.Y reference."""
    input_name:        str   # the input attribute being wired (e.g., "vpc_id")
    provider_service:  str   # service_name of the module that provides this
                              # (e.g., "vpc" service emits the vpc module)
    provider_output:   str   # the output key on the provider module
                              # (e.g., "vpc_ids" output of the vpc module)
    todo_placeholder:  str   # the literal TODO string the translator wrote
    # Conversion to apply to the module output before consumption:
    #   "scalar_first" → `values(module.X.Y)[0]` (pick first entry of a map)
    #   "list_values"  → `values(module.X.Y)` (map → list of values)
    #   "raw"          → `module.X.Y` (no conversion; use output directly)
    convert: str = "raw"


# The wiring table. Each entry is a (input_attribute) → (provider, output) edge.
# The provider module outputs are maps (keyed by resource-name), so we
# convert to scalar (first entry) or list based on what the consumer expects.
_WIRING_RULES: List[WiringRule] = [
    # ---- Networking (vpc → everywhere) ----
    # vpc_id consumers expect scalar → pick first entry from the vpc_ids map.
    # Operator can refine post-emission if env has multiple VPCs.
    WiringRule(
        input_name="vpc_id",
        provider_service="vpc",
        provider_output="vpc_ids",
        todo_placeholder="vpc-TODO",
        convert="scalar_first",
    ),
    WiringRule(
        input_name="vpc_id",
        provider_service="vpc",
        provider_output="vpc_ids",
        todo_placeholder="TODO-vpc-id",
        convert="scalar_first",
    ),
    # subnet_ids consumers expect list(string) — vpc module's `subnet_ids`
    # is a map(string), so we wrap in values() to convert to list.
    WiringRule(
        input_name="subnet_ids",
        provider_service="vpc",
        provider_output="subnet_ids",
        todo_placeholder="",   # subnet_ids = [] is the placeholder shape
        convert="list_values",
    ),
    WiringRule(
        input_name="subnet_ids",
        provider_service="subnet",
        provider_output="subnet_ids",
        todo_placeholder="",
        convert="list_values",
    ),
    # ---- ALB → ACM cert ----
    # ssl_certificate_arn consumer expects a scalar → pick first cert.
    # Multi-cert envs need operator refinement (pick the right key).
    WiringRule(
        input_name="ssl_certificate_arn",
        provider_service="acm-certificate",
        provider_output="certificate_arns",
        todo_placeholder="TODO-acm-cert-arn",
        convert="scalar_first",
    ),
    # ---- EventBridge Scheduler → SNS topic ----
    WiringRule(
        input_name="target_arn",
        provider_service="sns-sqs-fanout",
        provider_output="topic_arns",
        todo_placeholder="TODO",
        convert="scalar_first",
    ),
    # ---- EKS → KMS key for secrets (when not already in module) ----
    # (intentionally not auto-wired — EKS module creates its own KMS)
]


def _convert_reference(module_ref: str, convert: str) -> str:
    """Apply the conversion wrapper to a module.X.Y reference."""
    if convert == "scalar_first":
        return f"values({module_ref})[0]"
    if convert == "list_values":
        return f"values({module_ref})"
    return module_ref


def _build_provider_lookup(modules_in_env: List[Tuple[str, str]]) -> Dict[str, str]:
    """Map service_name → first block_name emitting that service.

    Used to resolve module.<block_name>.<output> references. If the env
    has multiple modules of the same service (e.g., two ALB modules),
    the first one wins for now — operator can rewire after-the-fact.
    """
    out: Dict[str, str] = {}
    for block_name, service_name in modules_in_env:
        if service_name not in out:
            out[service_name] = block_name
    return out


def rewrite_inputs(
    aws_inputs_hcl: str,
    *,
    modules_in_env: List[Tuple[str, str]],
) -> str:
    """Apply cross-module wiring rewrites to a single module call's inputs.

    Args:
        aws_inputs_hcl: the rendered inputs block from a translator
            (text starting with `  some_attr = ...` lines, going inside
            the `module "X" { ... }` body).
        modules_in_env: list of (block_name, service_name) tuples for
            EVERY module call in this env's main.tf. The function uses
            this to know which references are resolvable.

    Returns: the rewritten HCL with TODOs replaced by module.X.Y refs
    where possible. Unresolved TODOs left in place.
    """
    provider_lookup = _build_provider_lookup(modules_in_env)
    out = aws_inputs_hcl

    for rule in _WIRING_RULES:
        provider_block = provider_lookup.get(rule.provider_service)
        if provider_block is None:
            # No provider for this rule in this env — skip.
            continue

        base_ref = f"module.{provider_block}.{rule.provider_output}"
        replacement_ref = _convert_reference(base_ref, rule.convert)

        # Pattern 1: literal TODO placeholder string.
        # Source: input_name = "TODO-X"  →  input_name = <converted-ref>
        if rule.todo_placeholder:
            # Match: <input_name> = "<todo>"  (possibly indented)
            pat = re.compile(
                rf'(\b{re.escape(rule.input_name)}\s*=\s*)"{re.escape(rule.todo_placeholder)}"'
            )
            out = pat.sub(rf"\1{replacement_ref}", out)

        # Pattern 2: empty-list placeholder for list-typed inputs.
        # Source: subnet_ids = []  →  subnet_ids = <converted-ref>
        if rule.todo_placeholder == "":
            empty_list_pat = re.compile(
                rf'(\b{re.escape(rule.input_name)}\s*=\s*)\[\s*\]'
            )
            out = empty_list_pat.sub(rf"\1{replacement_ref}", out)

    return out


def list_wired_inputs() -> List[str]:
    """Return the set of input names this module knows how to wire.

    Used in the per-env header comment so operators know which TODOs
    were auto-resolved vs left for manual review.
    """
    return sorted({r.input_name for r in _WIRING_RULES})
