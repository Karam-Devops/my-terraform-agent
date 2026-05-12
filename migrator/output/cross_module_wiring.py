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
    # Cross-env fallback variable name. When the provider service is
    # NOT in this env (so the in-env wiring can't fire), the rewriter
    # substitutes `var.<cross_env_var>` instead of leaving the bare
    # TODO string. The emitter is responsible for declaring the
    # variable in each env's variables.tf so terraform validate stays
    # green.
    # Use case (DH terarecon env): ALB references an ACM cert that's
    # defined in a DIFFERENT env. We can't generate a module ref, so
    # we emit `var.ssl_certificate_arn` and the operator passes it via
    # tfvars or workspace inputs. Same idea as cross-stack outputs in
    # CloudFormation.
    cross_env_var: Optional[str] = None
    # Name of the INPUT attribute on the provider module whose top-level
    # keys correspond to this output. When set + convert="scalar_first",
    # the rewriter emits `module.X.<output>["<key>"]` instead of
    # `values(module.X.<output>)[0]`. The key is picked by name overlap
    # with the consumer block.
    # Example: VPC translator emits `vpcs = { "vpc_nfr_shared" = ..., }`
    # AND `vpc_ids = { for k, v in aws_vpc.this : k => v.id }`, so the
    # output keys ARE the input map keys. Setting provider_input_map=
    # "vpcs" lets wiring resolve which specific VPC each consumer wants.
    # When unset, falls back to values(...)[0] / values(...) wrapping.
    provider_input_map: Optional[str] = None


# The wiring table. Each entry is a (input_attribute) → (provider, output) edge.
# The provider module outputs are maps (keyed by resource-name), so we
# convert to scalar (first entry) or list based on what the consumer expects.
_WIRING_RULES: List[WiringRule] = [
    # ---- Networking (vpc → everywhere) ----
    # vpc_id consumers expect scalar. provider_input_map="vpcs" tells
    # the wiring layer to pick a specific key from the chosen VPC
    # module's `vpcs` input (which becomes the `vpc_ids` output map),
    # via consumer-name overlap. Emits module.X.vpc_ids["picked_key"]
    # instead of the fragile values(...)[0].
    WiringRule(
        input_name="vpc_id",
        provider_service="vpc",
        provider_output="vpc_ids",
        todo_placeholder="vpc-TODO",
        convert="scalar_first",
        provider_input_map="vpcs",
    ),
    WiringRule(
        input_name="vpc_id",
        provider_service="vpc",
        provider_output="vpc_ids",
        todo_placeholder="TODO-vpc-id",
        convert="scalar_first",
        provider_input_map="vpcs",
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
    # Scalar consumer; pick the right cert by name overlap when the
    # acm module has multiple certs in its `certificates` map.
    # cross_env_var fallback: when the env has an ALB but no ACM
    # module (e.g., DH's terarecon env where the cert lives in a
    # shared-infra env), substitute `var.ssl_certificate_arn` instead
    # of leaving the bare TODO. Operator supplies the ARN via tfvars.
    WiringRule(
        input_name="ssl_certificate_arn",
        provider_service="acm-certificate",
        provider_output="certificate_arns",
        todo_placeholder="TODO-acm-cert-arn",
        convert="scalar_first",
        cross_env_var="ssl_certificate_arn",
        provider_input_map="certificates",
    ),
    # ---- EventBridge Scheduler → SNS topic ----
    # The scheduler translator writes `target_arn = "TODO-target-arn"`
    # per schedule entry; that's the exact placeholder we substitute.
    # When the env has multiple SNS modules, provider_input_map="topics"
    # tells wiring to pick the topic whose name overlaps the consumer
    # block (e.g., scheduler `dh_vm_automate_topic` → SNS module that
    # has key `dh_vm_automate_topic` in its `topics` map).
    WiringRule(
        input_name="target_arn",
        provider_service="sns-sqs-fanout",
        provider_output="topic_arns",
        todo_placeholder="TODO-target-arn",
        convert="scalar_first",
        provider_input_map="topics",
    ),
    # ---- EKS → KMS key for secrets (when not already in module) ----
    # (intentionally not auto-wired — EKS module creates its own KMS)
]


def _convert_reference(module_ref: str, convert: str, *,
                       indexed_key: Optional[str] = None) -> str:
    """Apply the conversion wrapper to a module.X.Y reference.

    When ``indexed_key`` is given AND convert="scalar_first", emit a
    named-key lookup `module.X.Y["key"]` (more legible + correct than
    the fragile values()[0]). Without a key, fall back to values()[0].
    """
    if convert == "scalar_first":
        if indexed_key:
            return f'{module_ref}["{indexed_key}"]'
        return f"values({module_ref})[0]"
    if convert == "list_values":
        return f"values({module_ref})"
    return module_ref


def extract_top_level_map_keys(hcl: str, attr_name: str) -> List[str]:
    """Find the top-level map keys inside `<attr_name> = { ... }`.

    Walks the string character-by-character tracking brace depth so
    nested maps don't get mis-identified as top-level keys. Returns
    keys in source order. Handles both quoted ("vpc_dev_shared") and
    bare identifier (vpc_dev_shared) key forms — translators emit both
    depending on what's a valid HCL identifier.

    Pure stdlib — no HCL parser dep, so it works on partial inputs
    blocks (which is what we pass from the rendered translation).
    """
    keys: List[str] = []
    # Find `<attr> = {` with optional whitespace flexibility.
    # We search line-by-line so we don't match `something_else = {` that
    # happens to end with our attr name.
    pat = re.compile(rf'(?m)^\s*{re.escape(attr_name)}\s*=\s*\{{')
    m = pat.search(hcl)
    if not m:
        return keys
    start = m.end() - 1  # position of the opening brace
    depth = 0
    i = start
    line_start = i + 1
    n = len(hcl)
    while i < n:
        ch = hcl[i]
        if ch == '{':
            depth += 1
            if depth == 1:
                line_start = i + 1
        elif ch == '}':
            depth -= 1
            if depth == 0:
                # End of the attr's map block
                break
        elif ch == '\n' and depth == 1:
            line = hcl[line_start:i]
            line_start = i + 1
            _maybe_capture_key(line, keys)
        i += 1
    # Capture a trailing key if there was no terminal newline
    if depth >= 1:
        line = hcl[line_start:i]
        _maybe_capture_key(line, keys)
    return keys


def _maybe_capture_key(line: str, keys: List[str]) -> None:
    """If ``line`` is a top-level `"key" = {...}` or `key = {...}`
    entry, append the key. Skips blanks + comments + non-assignments."""
    stripped = line.strip()
    if not stripped or stripped.startswith('#'):
        return
    # Quoted key
    if stripped.startswith('"'):
        close = stripped.find('"', 1)
        if close > 0:
            after = stripped[close + 1:].lstrip()
            if after.startswith('='):
                keys.append(stripped[1:close])
        return
    # Bare identifier key — `name = { ... }`
    eq = stripped.find('=')
    if eq <= 0:
        return
    name = stripped[:eq].strip()
    # Require name to be a valid HCL identifier (letters, digits, underscores)
    if name and (name[0].isalpha() or name[0] == '_') and all(
        c.isalnum() or c == '_' for c in name
    ):
        keys.append(name)


def _pick_key_by_overlap(
    candidates: List[str],
    *,
    consumer_block_name: Optional[str],
) -> Optional[str]:
    """Among a provider's output keys, pick the one whose name shares
    the most tokens with the consumer's block name. Ties broken by
    shorter key (less specialized). Returns None when ``candidates``
    is empty.

    Example: consumer `scheduler_dh_vm_automate` + candidates
    [`dh_vm_automate_topic`, `dh_health_check`, `dh_billing`]
    → picks `dh_vm_automate_topic` (shares `dh_vm_automate`).
    """
    if not candidates:
        return None
    if len(candidates) == 1 or not consumer_block_name:
        return candidates[0]
    return max(
        candidates,
        key=lambda k: (_shared_token_count(k, consumer_block_name), -len(k)),
    )


def _build_provider_lookup(
    modules_in_env: List[Tuple[str, str]],
    *,
    consumer_block_name: Optional[str] = None,
) -> Dict[str, str]:
    """Map service_name → best block_name emitting that service.

    Used to resolve module.<block_name>.<output> references. When the
    env has MULTIPLE modules of the same service (e.g., DH's
    common-network env has 8 vpc modules: vpc, artifact_registry, ncc,
    net_address, pubsub, sa_iam_bindings, secrets, serverless_vpc),
    the original "first wins" picked `compute_network_artifact_registry`
    for every consumer — clearly wrong.

    Better heuristic:
      1. If consumer_block_name is given, prefer the provider block
         whose name shares the longest common substring with the
         consumer. e.g., `compute_instance_dev_workload` → prefer
         `compute_network_vpc_dev` over the artifact-registry one.
      2. Otherwise rank candidates by "canonical-ness":
            * Block names ending in `_vpc` / `_network` win first
            * Shorter block names tied second (less specialized)
      3. Stable when nothing distinguishes candidates: first-seen wins
         (preserves backwards compatibility for single-provider envs).
    """
    # Group by service_name → list of candidate block_names
    by_service: Dict[str, List[str]] = {}
    for block_name, service_name in modules_in_env:
        by_service.setdefault(service_name, []).append(block_name)

    out: Dict[str, str] = {}
    for service_name, candidates in by_service.items():
        if len(candidates) == 1:
            out[service_name] = candidates[0]
            continue
        # Multi-candidate: pick the best match.
        out[service_name] = _pick_best_provider(
            candidates, consumer_block_name=consumer_block_name,
        )
    return out


def _pick_best_provider(
    candidates: List[str],
    *,
    consumer_block_name: Optional[str],
) -> str:
    """Rank multiple provider blocks of the same service and pick one.

    Three-tier ranking:
      1. Highest substring overlap with consumer_block_name (when set)
      2. Highest "canonical" score (ends in _vpc/_network → bonus)
      3. Shortest block name (tiebreaker — less specialized)
    """
    def score(block_name: str) -> Tuple[int, int, int]:
        # Tier 1: longest shared substring with consumer
        overlap = 0
        if consumer_block_name:
            overlap = _shared_token_count(block_name, consumer_block_name)
        # Tier 2: canonical-name bonus
        canon = 0
        if block_name.endswith(("_vpc", "_network", "_main", "_primary")):
            canon = 2
        elif "_vpc_" in block_name or "_network_" in block_name:
            canon = 1
        # Tier 3: short names beat long names (negate so higher = better)
        brevity = -len(block_name)
        return (overlap, canon, brevity)

    return max(candidates, key=score)


def _shared_token_count(a: str, b: str) -> int:
    """Count tokens (underscore-split) that appear in both names.

    Used to detect that `compute_instance_dev_workload` is "related"
    to `compute_network_vpc_dev_shared` (both have `compute` and `dev`).
    The more overlapping tokens, the stronger the relationship.
    """
    tokens_a = set(a.split("_"))
    tokens_b = set(b.split("_"))
    # Drop generic prefixes that match every block — they don't carry
    # signal about which specific provider this consumer wants.
    generic = {"compute", "module", "google", ""}
    return len((tokens_a & tokens_b) - generic)


def rewrite_inputs(
    aws_inputs_hcl: str,
    *,
    modules_in_env: List[Tuple[str, str]],
    consumer_block_name: Optional[str] = None,
    provider_output_keys: Optional[Dict[str, Dict[str, List[str]]]] = None,
) -> str:
    """Apply cross-module wiring rewrites to a single module call's inputs.

    Args:
        aws_inputs_hcl: the rendered inputs block from a translator
            (text starting with `  some_attr = ...` lines, going inside
            the `module "X" { ... }` body).
        modules_in_env: list of (block_name, service_name) tuples for
            EVERY module call in this env's main.tf. The function uses
            this to know which references are resolvable.
        consumer_block_name: name of the module {} block whose inputs
            are being rewritten. When the env has MULTIPLE provider
            modules of the same service (e.g., multiple VPCs), the
            provider lookup uses this to pick the closest-named one
            via token-overlap heuristic. Without this hint, "first seen"
            wins — which produced the bug Kiro flagged where every
            consumer in common-network ended up wired to the
            artifact-registry VPC instead of the right one.

    Returns: the rewritten HCL with TODOs replaced by module.X.Y refs
    when both sides are in the env. When the provider module is NOT
    in the env but the rule declares a ``cross_env_var``, the TODO is
    replaced by ``var.<cross_env_var>`` so the operator can supply
    the value via tfvars or a workspace variable. Otherwise the TODO
    is left in place for manual resolution.
    """
    provider_lookup = _build_provider_lookup(
        modules_in_env, consumer_block_name=consumer_block_name,
    )
    # Per-service list of ALL candidate provider blocks (used to surface
    # alternatives in a comment when the heuristic had to choose among
    # multiple). DH's common-network env has 8 VPC modules — the
    # operator should see what other options exist so they can rewire
    # if the heuristic picked the wrong one.
    candidates_by_service: Dict[str, List[str]] = {}
    for block_name, service_name in modules_in_env:
        candidates_by_service.setdefault(service_name, []).append(block_name)

    out = aws_inputs_hcl

    for rule in _WIRING_RULES:
        provider_block = provider_lookup.get(rule.provider_service)

        if provider_block is not None:
            base_ref = f"module.{provider_block}.{rule.provider_output}"
            # Named-key lookup when the rule declares which input map
            # carries the output keys AND the emitter handed us the
            # extracted keys for that map. Picks the key whose name
            # shares the most tokens with the consumer block — handles
            # the multi-VPC / multi-SNS-topic cases Kiro flagged.
            indexed_key: Optional[str] = None
            if (
                rule.convert == "scalar_first"
                and rule.provider_input_map
                and provider_output_keys
            ):
                provider_maps = provider_output_keys.get(provider_block) or {}
                candidate_keys = provider_maps.get(rule.provider_input_map) or []
                indexed_key = _pick_key_by_overlap(
                    candidate_keys, consumer_block_name=consumer_block_name,
                )
            replacement_ref = _convert_reference(
                base_ref, rule.convert, indexed_key=indexed_key,
            )
        elif rule.cross_env_var:
            # No in-env provider, but the rule has a cross-env fallback.
            # Emit a `var.X` reference and let the operator wire the
            # actual value via tfvars / workspace variables / remote
            # state. The emitter declares the variable in variables.tf
            # so terraform validate stays green.
            replacement_ref = f"var.{rule.cross_env_var}"
        else:
            # No in-env provider AND no cross-env fallback — leave the
            # TODO for manual resolution.
            continue

        # When multiple provider blocks existed for this service AND we
        # chose one via heuristic, append an inline "alternatives"
        # comment so the operator can rewire to a different one without
        # having to scan the file. Only fires when the in-env path
        # actually picked a module (not the cross_env_var fallback).
        alternative_hint = ""
        if provider_block is not None:
            alts = candidates_by_service.get(rule.provider_service, [])
            if len(alts) > 1:
                others = [a for a in alts if a != provider_block]
                alternative_hint = (
                    f"  # auto-picked among {len(alts)} {rule.provider_service} "
                    f"modules; alternatives: {', '.join(others[:3])}"
                    f"{'...' if len(others) > 3 else ''}"
                )

        # Pattern 1: literal TODO placeholder string.
        # Source: input_name = "TODO-X"  →  input_name = <converted-ref>
        if rule.todo_placeholder:
            # Match: <input_name> = "<todo>"  (possibly indented)
            pat = re.compile(
                rf'(\b{re.escape(rule.input_name)}\s*=\s*)"{re.escape(rule.todo_placeholder)}"'
            )
            out = pat.sub(rf"\1{replacement_ref}{alternative_hint}", out)

        # Pattern 2: empty-list placeholder for list-typed inputs.
        # Source: subnet_ids = []  →  subnet_ids = <converted-ref>
        if rule.todo_placeholder == "":
            empty_list_pat = re.compile(
                rf'(\b{re.escape(rule.input_name)}\s*=\s*)\[\s*\]'
            )
            out = empty_list_pat.sub(rf"\1{replacement_ref}{alternative_hint}", out)

    return out


def cross_env_vars_referenced(
    aws_inputs_hcl: str,
    *,
    modules_in_env: List[Tuple[str, str]],
) -> List[str]:
    """Return the cross-env var names that WOULD be substituted into
    this inputs block. The emitter uses this to know which `variable`
    declarations to add to each env's variables.tf so the references
    resolve and ``terraform validate`` stays green.

    Pure function — no side effects. Doesn't actually rewrite the HCL,
    just predicts which rules would trip the cross_env_var branch.
    """
    provider_lookup = _build_provider_lookup(modules_in_env)
    needed: List[str] = []
    for rule in _WIRING_RULES:
        if rule.cross_env_var is None:
            continue
        if rule.provider_service in provider_lookup:
            continue
        if not rule.todo_placeholder:
            continue
        # Only declare the var if the corresponding TODO actually
        # appears in this block — otherwise we'd over-declare and
        # leave unused vars in variables.tf.
        if f'"{rule.todo_placeholder}"' in aws_inputs_hcl:
            if rule.cross_env_var not in needed:
                needed.append(rule.cross_env_var)
    return needed


def list_wired_inputs() -> List[str]:
    """Return the set of input names this module knows how to wire.

    Used in the per-env header comment so operators know which TODOs
    were auto-resolved vs left for manual review.
    """
    return sorted({r.input_name for r in _WIRING_RULES})
