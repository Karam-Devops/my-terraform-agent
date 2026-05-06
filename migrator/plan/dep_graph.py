"""Build a resource dependency graph by scanning HCL argument values
for inter-resource references.

A reference looks like ``<tf_type>.<name>.<attr>`` inside an argument
value (e.g. ``network = google_compute_network.vpc.id``). We extract
the (tf_type, name) pair and emit a DependencyEdge from the holding
resource to the target.

This is best-effort string scanning — it does NOT execute HCL
expressions or resolve variables, locals, or modules. Sufficient
for ordering, not for semantic equivalence.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Set, Tuple

from migrator.results import DependencyEdge, DiscoveredResource


# Matches `<tf_type>.<name>.<attr>` where tf_type starts with a letter,
# both labels are HCL-identifier-shaped (alpha + alnum/underscore), and
# `.<attr>` is at least one character.
#
# Constrained to provider-prefixed types (google_*, google-beta_*,
# aws_*) so we don't false-match Python module references that find
# their way into comments. Add new prefixes as we expand coverage.
_REF_RE = re.compile(
    r"\b(?P<tf_type>(?:google|google-beta|aws|azurerm)_[A-Za-z0-9_]+)"
    r"\.(?P<name>[A-Za-z][A-Za-z0-9_-]*)"
    r"\.(?P<attr>[A-Za-z][A-Za-z0-9_]*(?:\[\d+\])?(?:\.[A-Za-z0-9_]+)*)"
)


def build_dep_graph(resources: List[DiscoveredResource]) -> List[DependencyEdge]:
    """Scan every argument value of every resource for cross-references.

    Args:
        resources: flat list from ingest.

    Returns:
        Sorted list of DependencyEdge — one edge per (source -> target)
        unique reference. Multiple references to the same target via
        different attrs collapse to one edge labelled with the first
        attr seen (sufficient for ordering).
    """
    # Set-of-(source, target) for dedup, plus a parallel attr lookup
    # so the rendered edge keeps the first attr we saw.
    address_set: Set[str] = {r.address for r in resources}
    seen: Set[Tuple[str, str]] = set()
    first_attr: Dict[Tuple[str, str], str] = {}

    for r in resources:
        source_addr = r.address
        for attr_path, target_addr in _iter_refs(r.arguments):
            if target_addr == source_addr:
                # Self-reference (rare but possible in dynamic blocks)
                continue
            # Only emit edges to resources we actually discovered.
            # Cross-module references the parser couldn't resolve get
            # silently dropped (they'd add noise to the graph).
            if target_addr not in address_set:
                continue
            key = (source_addr, target_addr)
            if key in seen:
                continue
            seen.add(key)
            first_attr[key] = attr_path

    edges = [
        DependencyEdge(source=src, target=tgt, via=first_attr[(src, tgt)])
        for (src, tgt) in seen
    ]
    edges.sort(key=lambda e: (e.source, e.target))
    return edges


def topological_order(
    resources: List[DiscoveredResource],
    edges: List[DependencyEdge],
) -> List[str]:
    """Return resource addresses in dependency-first order.

    Edges go source→target where source DEPENDS ON target, so we want
    targets before sources in the output (deploy targets first, then
    things that reference them). Cycles are broken arbitrarily — a
    real warning is logged elsewhere; we still produce a usable
    ordering for the migration guide.
    """
    # Build adj: dependent <- dependency (we deploy dependencies first)
    in_degree: Dict[str, int] = {r.address: 0 for r in resources}
    rev: Dict[str, List[str]] = {r.address: [] for r in resources}
    for e in edges:
        # `source` depends on `target`, so target must deploy first
        if e.source in in_degree:
            in_degree[e.source] += 1
        if e.target in rev:
            rev[e.target].append(e.source)

    # Kahn's algorithm.
    ready = [addr for addr, deg in in_degree.items() if deg == 0]
    ready.sort()
    out: List[str] = []
    while ready:
        addr = ready.pop(0)
        out.append(addr)
        for dependent in sorted(rev.get(addr, [])):
            in_degree[dependent] -= 1
            if in_degree[dependent] == 0:
                ready.append(dependent)
                ready.sort()

    # Cycle leftovers — append in stable order so output is deterministic.
    leftover = sorted(addr for addr, deg in in_degree.items() if deg > 0)
    out.extend(leftover)
    return out


def _iter_refs(node: Any, _path: str = ""):
    """Recursively walk an HCL argument tree, yielding (attr_path, ref_addr).

    Handles nested dicts, lists, and string scalars. python-hcl2 emits
    expressions sometimes as raw strings (with `${...}` markers) and
    sometimes as already-evaluated literals; the regex catches both.
    """
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
    # ints, floats, bools, None — no refs possible.
