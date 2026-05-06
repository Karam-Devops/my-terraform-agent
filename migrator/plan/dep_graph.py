"""Build a resource dependency graph.

Two sources of edges:

  1. **Inline references** in vanilla Terraform argument values: a
     string like ``google_compute_network.vpc.id`` inside an argument
     creates an edge from the holding resource to that target.

  2. **Terragrunt `dependencies { paths = [...] }` blocks**: each
     relative path resolves to a sibling stack's directory; the
     resource discovered at that directory becomes the edge target.

The graph is keyed on each resource's ``qualified_id`` (= module_path
+ address) so duplicate addresses across environments don't collapse.
The migration guide / UI then can map qualified_id → resource for
display.
"""

from __future__ import annotations

import os
import re
from typing import Any, Dict, List, Set, Tuple

from migrator.results import DependencyEdge, DiscoveredResource


# Provider-prefixed inline reference pattern: `<tf_type>.<name>.<attr>`.
_REF_RE = re.compile(
    r"\b(?P<tf_type>(?:google|google-beta|aws|azurerm)_[A-Za-z0-9_]+)"
    r"\.(?P<name>[A-Za-z][A-Za-z0-9_-]*)"
    r"\.(?P<attr>[A-Za-z][A-Za-z0-9_]*(?:\[\d+\])?(?:\.[A-Za-z0-9_]+)*)"
)


def build_dep_graph(resources: List[DiscoveredResource]) -> List[DependencyEdge]:
    """Build the cross-resource dependency edge list.

    Edges are emitted in the operator-facing form:
        DependencyEdge(source=..., target=..., via=...)
    where source/target are resource ``address`` strings (not
    qualified_id) — operator-facing display uses simpler addresses.
    For Terragrunt-mode the same address can repeat across envs;
    the (source, target, source_module, target_module) tuple is
    deduplicated internally before flattening.
    """
    # Address-based set for inline refs (vanilla TF mode).
    address_set: Set[str] = {r.address for r in resources}

    # module_path → first DiscoveredResource at that path. In Terragrunt
    # mode each leaf stack's terragrunt.hcl produces exactly one synthetic
    # resource, so this is a 1:1 lookup.
    by_module: Dict[str, DiscoveredResource] = {}
    for r in resources:
        by_module.setdefault(r.module_path, r)

    seen: Set[Tuple[str, str, str]] = set()    # (src_addr, tgt_addr, via)
    edges: List[DependencyEdge] = []

    for r in resources:
        source_addr = r.address

        # ----- inline refs in argument values -----
        for attr_path, target_addr in _iter_refs(r.arguments):
            if target_addr == source_addr:
                continue
            if target_addr not in address_set:
                continue
            key = (source_addr, target_addr, attr_path)
            if key in seen:
                continue
            seen.add(key)
            edges.append(DependencyEdge(
                source=source_addr, target=target_addr, via=attr_path,
            ))

        # ----- Terragrunt `dependencies { paths = [...] }` -----
        for rel_path in r.terragrunt_deps:
            target_module = _resolve_relative(r.module_path, rel_path)
            target_resource = by_module.get(target_module)
            if target_resource is None:
                continue  # path doesn't resolve to a discovered stack
            target_addr = target_resource.address
            if target_addr == source_addr:
                continue
            via = f"terragrunt:{rel_path}"
            key = (source_addr, target_addr, via)
            if key in seen:
                continue
            seen.add(key)
            edges.append(DependencyEdge(
                source=source_addr, target=target_addr, via=via,
            ))

    edges.sort(key=lambda e: (e.source, e.target, e.via))
    return edges


def topological_order(
    resources: List[DiscoveredResource],
    edges: List[DependencyEdge],
) -> List[str]:
    """Return resource ``qualified_id`` strings in deploy-first order.

    Edges go source→target where source DEPENDS ON target, so we want
    targets before sources in the output (deploy targets first, then
    things that reference them).

    Keyed on ``qualified_id`` so duplicate addresses across environments
    don't collapse — each leaf stack appears in the order independently.

    Cycles are broken arbitrarily; cycle members appear at the end of
    the list in stable alphabetical order so output stays deterministic.
    """
    # Build per-qualified-id in_degree + reverse adjacency.
    by_qid: Dict[str, DiscoveredResource] = {r.qualified_id: r for r in resources}
    in_degree: Dict[str, int] = {qid: 0 for qid in by_qid}
    rev: Dict[str, List[str]] = {qid: [] for qid in by_qid}

    # Edges are address-based; map them back to qualified_id where
    # possible. When an address appears in multiple modules (collision),
    # we add the edge from EVERY source instance to EVERY target
    # instance — that's intentional: in Terragrunt mode the same
    # logical "depends-on" applies across all duplicates.
    by_address: Dict[str, List[str]] = {}
    for r in resources:
        by_address.setdefault(r.address, []).append(r.qualified_id)

    for e in edges:
        src_qids = by_address.get(e.source, [])
        tgt_qids = by_address.get(e.target, [])
        for src in src_qids:
            for tgt in tgt_qids:
                if src == tgt:
                    continue
                in_degree[src] = in_degree.get(src, 0) + 1
                rev.setdefault(tgt, []).append(src)

    # Kahn's algorithm.
    ready = sorted(qid for qid, deg in in_degree.items() if deg == 0)
    out: List[str] = []
    while ready:
        qid = ready.pop(0)
        out.append(qid)
        for dependent in sorted(rev.get(qid, [])):
            if in_degree.get(dependent, 0) > 0:
                in_degree[dependent] -= 1
                if in_degree[dependent] == 0:
                    ready.append(dependent)
                    ready.sort()

    # Cycle leftovers — append in stable order so output is deterministic.
    leftover = sorted(qid for qid, deg in in_degree.items() if deg > 0)
    out.extend(leftover)
    return out


# -----------------------------------------------------------------
# helpers
# -----------------------------------------------------------------

def _iter_refs(node: Any, _path: str = ""):
    """Walk an HCL argument tree, yielding (attr_path, ref_addr)."""
    if isinstance(node, str):
        for m in _REF_RE.finditer(node):
            tf_type = m.group("tf_type")
            name = m.group("name")
            attr = m.group("attr")
            yield (attr, f"{tf_type}.{name}")
        return
    if isinstance(node, dict):
        for k, v in node.items():
            yield from _iter_refs(v, f"{_path}.{k}" if _path else k)
        return
    if isinstance(node, (list, tuple)):
        for i, v in enumerate(node):
            yield from _iter_refs(v, f"{_path}[{i}]")
        return


def _resolve_relative(base_module_path: str, rel_path: str) -> str:
    """Resolve a Terragrunt-style `../foo` relative to a module_path.

    Both paths are repo-relative POSIX strings (forward slashes).
    Returns the normalized target module_path.
    """
    # os.path.normpath collapses `..` and `.`; we then convert
    # back to forward slashes for cross-platform stability.
    combined = os.path.normpath(os.path.join(base_module_path, rel_path))
    return combined.replace(os.sep, "/")
