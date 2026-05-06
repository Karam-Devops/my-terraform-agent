"""Render MIGRATION_GUIDE.md + sidecar migration_plan.json.

The guide is the headline operator-facing deliverable: ordered
deployment sequence with pre-deploy checks, per-resource confidence,
and rollback notes. The JSON sidecar is the same content in a
machine-readable shape so downstream tooling (Streamlit page, future
GitHub PR description) can render it however it needs.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Dict, List, Optional

from migrator.plan.dep_graph import topological_order
from migrator.results import (
    CONFIDENCE_BANDS,
    CONFIDENCE_HIGH,
    CONFIDENCE_LOW,
    CONFIDENCE_MANUAL,
    CONFIDENCE_MEDIUM,
    ConfidenceFinding,
    DependencyEdge,
    DiscoveredResource,
)

# Cap on how many "lowest-confidence" resources get a per-resource
# notes block. With 941 resources, the per-resource section is
# unreadable as a wall of text; we focus the operator on the items
# that need attention.
_PER_RESOURCE_NOTES_CAP = 50


def emit_migration_guide(
    *,
    output_dir: str,
    repo_path: str,
    target_cloud: str,
    source_iac: str,
    resources: List[DiscoveredResource],
    confidence: List[ConfidenceFinding],
    dep_edges: List[DependencyEdge],
) -> str:
    """Write MIGRATION_GUIDE.md (and migration_plan.json) under output_dir."""
    os.makedirs(output_dir, exist_ok=True)

    confidence_by_addr = {c.resource_address: c for c in confidence}

    # qualified_id-keyed deploy order (so 941 doesn't collapse to 134
    # via address collision in Terragrunt mode).
    deploy_qids = topological_order(resources, dep_edges)
    by_qid = {r.qualified_id: r for r in resources}

    # For each resource, list the addresses it depends on (= edges
    # whose source is this resource's address).
    deps_by_source: Dict[str, List[str]] = {}
    for e in dep_edges:
        deps_by_source.setdefault(e.source, []).append(e.target)

    summary = {b: 0 for b in CONFIDENCE_BANDS}
    for c in confidence:
        if c.band in summary:
            summary[c.band] += 1

    timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds")

    md_path = os.path.join(output_dir, "MIGRATION_GUIDE.md")
    with open(md_path, "w", encoding="utf-8") as fh:
        fh.write(_render_markdown(
            timestamp=timestamp,
            repo_path=repo_path,
            target_cloud=target_cloud,
            source_iac=source_iac,
            resources=resources,
            confidence_by_addr=confidence_by_addr,
            deploy_qids=deploy_qids,
            by_qid=by_qid,
            deps_by_source=deps_by_source,
            summary=summary,
        ))

    # JSON sidecar — same content, machine-readable shape.
    json_path = os.path.join(output_dir, "migration_plan.json")
    with open(json_path, "w", encoding="utf-8") as fh:
        json.dump({
            "generated_at_utc": timestamp,
            "repo_path": os.path.abspath(repo_path),
            "target_cloud": target_cloud,
            "source_iac": source_iac,
            "summary": summary,
            "deploy_order_qualified_ids": deploy_qids,
            "resources": [
                {
                    "qualified_id": r.qualified_id,
                    "address": r.address,
                    "tf_type": r.tf_type,
                    "name": r.name,
                    "module_path": r.module_path,
                    "depends_on_addresses": deps_by_source.get(r.address, []),
                    "confidence": _conf_to_dict(confidence_by_addr.get(r.address)),
                }
                for r in resources
            ],
            "dependencies": [
                {"source": e.source, "target": e.target, "via": e.via}
                for e in dep_edges
            ],
        }, fh, indent=2)

    return md_path


# -----------------------------------------------------------------
# Markdown rendering
# -----------------------------------------------------------------

def _render_markdown(
    *,
    timestamp: str,
    repo_path: str,
    target_cloud: str,
    source_iac: str,
    resources: List[DiscoveredResource],
    confidence_by_addr: Dict[str, ConfidenceFinding],
    deploy_qids: List[str],
    by_qid: Dict[str, DiscoveredResource],
    deps_by_source: Dict[str, List[str]],
    summary: Dict[str, int],
) -> str:
    lines: List[str] = []
    lines.append(f"# Migration Guide — GCP → {target_cloud.upper()}")
    lines.append("")
    lines.append(f"_Generated: `{timestamp}`_")
    lines.append("")
    lines.append(f"**Source repo:** `{os.path.abspath(repo_path)}`")
    lines.append(f"**Source IaC:** `{source_iac}`")
    lines.append(f"**Target cloud:** `{target_cloud}`")
    lines.append(f"**Total resources:** {len(resources)}")
    lines.append("")
    lines.append("## Confidence summary")
    lines.append("")
    lines.append("| Band | Count |")
    lines.append("|---|---|")
    lines.append(f"| 🟢 HIGH (≥85%)         | {summary.get(CONFIDENCE_HIGH, 0)} |")
    lines.append(f"| 🟡 MEDIUM (60–84%)     | {summary.get(CONFIDENCE_MEDIUM, 0)} |")
    lines.append(f"| 🔴 LOW (<60%)          | {summary.get(CONFIDENCE_LOW, 0)} |")
    lines.append(f"| ⚠️  MANUAL REVIEW       | {summary.get(CONFIDENCE_MANUAL, 0)} |")
    lines.append("")
    lines.append("HIGH-band resources translate with minimal review. MEDIUM-band ")
    lines.append("require an engineer pass per resource. LOW-band involve paradigm ")
    lines.append("shifts (IAM model, IRSA, NEG topology) and need careful design. ")
    lines.append("MANUAL_REVIEW resources have no direct AWS equivalent.")
    lines.append("")

    # ---- Pre-deploy checklist ----
    lines.append("## Pre-deploy checklist")
    lines.append("")
    lines.append("- [ ] AWS landing zone is provisioned (Organizations / Control Tower / IAM Identity Center)")
    lines.append("- [ ] Target AWS region(s) confirmed and quotas raised where needed")
    lines.append("- [ ] Read-only access to source GCP project for reconciliation overlay")
    lines.append("- [ ] State backend (S3 + DynamoDB lock) exists in target AWS account")
    lines.append("- [ ] CI/CD pipeline service principal has deploy permissions in target account")
    lines.append("- [ ] DNS cutover plan agreed (Route 53 weighted routing during transition)")
    lines.append("- [ ] Backup/snapshot taken of every stateful GCP resource (Cloud SQL, GCS, Memorystore)")
    lines.append("- [ ] Rollback runbook reviewed and approved")
    lines.append("")

    # ---- Deployment sequence (dep-ordered, every resource visible) ----
    total = len(deploy_qids)
    lines.append(f"## Deployment sequence ({total} stacks)")
    lines.append("")
    lines.append("Deploy stacks in this order. Each row's **Depends on** column lists ")
    lines.append("the AWS resource addresses that must exist before this stack is ")
    lines.append("applied (resolved from Terragrunt `dependencies { paths = [...] }` ")
    lines.append("blocks plus inline `<tf_type>.<name>.<attr>` references).")
    lines.append("")
    if not deploy_qids:
        lines.append("_No resources discovered — nothing to deploy._")
        lines.append("")
    else:
        lines.append("| # | Stack | AWS equivalent | Confidence | Depends on | Module path |")
        lines.append("|---|---|---|---|---|---|")
        for i, qid in enumerate(deploy_qids, start=1):
            r = by_qid.get(qid)
            if r is None:
                continue
            conf = confidence_by_addr.get(r.address)
            aws_eq = (conf.aws_equivalent if conf and conf.aws_equivalent
                      else "_(manual review)_")
            band_label = _band_with_emoji(conf.band) if conf else "—"
            depends_on = deps_by_source.get(r.address, [])
            depends_str = (
                ", ".join(f"`{d}`" for d in depends_on[:3])
                + (f" +{len(depends_on)-3}" if len(depends_on) > 3 else "")
                if depends_on else "_(none)_"
            )
            lines.append(
                f"| {i} | `{r.address}` | `{aws_eq}` | "
                f"{band_label} | {depends_str} | `{r.module_path}` |"
            )
        lines.append("")

    # ---- Top N "needs attention" resources ----
    # Surface the lowest-confidence items so the operator knows where
    # to focus engineering review. Capped at _PER_RESOURCE_NOTES_CAP.
    confidences = [confidence_by_addr.get(r.address) for r in resources]
    confidences = [c for c in confidences if c is not None]
    confidences.sort(key=lambda c: (c.score_pct, c.resource_address))
    needs_attention = confidences[:_PER_RESOURCE_NOTES_CAP]

    if needs_attention:
        lines.append("## Resources needing attention")
        lines.append("")
        lines.append(f"The {len(needs_attention)} lowest-confidence resources are listed below. ")
        lines.append("Address these first during translation review. Higher-confidence ")
        lines.append("resources are listed in the Deployment sequence above and in the ")
        lines.append("`migration_plan.json` sidecar.")
        lines.append("")
        for c in needs_attention:
            lines.append(f"### `{c.resource_address}` — {_band_with_emoji(c.band)} ({c.score_pct}%)")
            lines.append("")
            aws_eq = c.aws_equivalent or "_(no AWS equivalent — manual review)_"
            lines.append(f"- **AWS equivalent:** `{aws_eq}`")
            lines.append(f"- **Reason:** {c.reason}")
            if c.notes:
                lines.append("- **Notes:**")
                for note in c.notes:
                    lines.append(f"  - {note}")
            lines.append("")

    # ---- Rollback ----
    lines.append("## Rollback procedure")
    lines.append("")
    lines.append("1. **Halt new traffic** — disable Route 53 weighted routing toward the AWS endpoints.")
    lines.append("2. **Drain in-flight workloads** — wait for active sessions to complete or fail over to GCP.")
    lines.append("3. **Re-enable GCP traffic** — restore original Route 53 / Cloud DNS records.")
    lines.append("4. **Preserve AWS state** — do NOT `terraform destroy` until root cause is identified.")
    lines.append("5. **Investigate** — the validation report and engine snapshots in this repo capture the failure surface.")
    lines.append("")
    lines.append("---")
    lines.append("_Migration guide generated by Cloud Lifecycle Intelligence — Migrator engine, from CitiusTech._")

    return "\n".join(lines) + "\n"


def _band_with_emoji(band: str) -> str:
    return {
        CONFIDENCE_HIGH:    "🟢 HIGH",
        CONFIDENCE_MEDIUM:  "🟡 MEDIUM",
        CONFIDENCE_LOW:     "🔴 LOW",
        CONFIDENCE_MANUAL:  "⚠️ MANUAL_REVIEW",
    }.get(band, band)


def _conf_to_dict(c: Optional[ConfidenceFinding]) -> Optional[Dict]:
    if c is None:
        return None
    return {
        "band": c.band,
        "score_pct": c.score_pct,
        "aws_equivalent": c.aws_equivalent,
        "reason": c.reason,
        "notes": list(c.notes),
    }
