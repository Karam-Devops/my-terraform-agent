"""Generate a customer-facing Markdown of the GCP→AWS mapping table.

Output: docs/MIGRATOR_COVERAGE.md (committed to the repo). Shows the
operator (and prospective customer) exactly what GCP resources the
Migrator translates, their AWS equivalents, confidence scores, and
whether a translator is actually registered today.

The mapping table is the single source of truth for "what we cover".
Today it lived only inside coverage.py — readable as Python but not
shareable in a sales deck or onboarding doc. This generator lifts it
out so the same data drives both engine behavior + customer docs.

Re-run after any change to _GCP_TO_AWS or TRANSLATORS:
    python -m migrator.plan.publish_mapping_table
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Dict, List


def generate_markdown() -> str:
    """Build the Markdown body. Pulls live data from coverage.py
    and translate/__init__.py so it's always in sync with the engine."""
    from migrator.plan.coverage import _GCP_TO_AWS, _band_for
    from migrator.translate import TRANSLATORS
    from migrator.results import (
        CONFIDENCE_HIGH, CONFIDENCE_MEDIUM, CONFIDENCE_LOW, CONFIDENCE_MANUAL,
    )

    # Group entries by band
    by_band: Dict[str, List] = {
        CONFIDENCE_HIGH:   [],
        CONFIDENCE_MEDIUM: [],
        CONFIDENCE_LOW:    [],
        CONFIDENCE_MANUAL: [],
    }
    for tf_type, entry in sorted(_GCP_TO_AWS.items()):
        band = _band_for(entry.score_pct)
        by_band[band].append((tf_type, entry))

    registered_types = set(TRANSLATORS.keys())
    timestamp = datetime.now(timezone.utc).isoformat(timespec="minutes")

    lines: List[str] = []
    lines.append("# Cloud Lifecycle Intelligence — Migrator Coverage")
    lines.append("")
    lines.append(f"_Generated: `{timestamp}` from `migrator/plan/coverage.py` + `migrator/translate/__init__.py`._")
    lines.append("")
    lines.append("This document is the **canonical answer** to *\"What GCP resources can the Migrator engine translate to AWS today?\"* "
                 "It's machine-generated from the engine's mapping table, so changes here reflect actual engine behavior — not aspirations.")
    lines.append("")

    # Summary at the top
    total = sum(len(v) for v in by_band.values())
    translated_count = sum(
        1 for tf_type in _GCP_TO_AWS
        if tf_type in registered_types and _band_for(_GCP_TO_AWS[tf_type].score_pct) != CONFIDENCE_MANUAL
    )
    lines.append("## Summary")
    lines.append("")
    lines.append("| Band | Count | Meaning |")
    lines.append("|---|---|---|")
    lines.append(f"| 🟢 **HIGH** (≥85%) | {len(by_band[CONFIDENCE_HIGH])} | Translates with minimal review |")
    lines.append(f"| 🟡 **MEDIUM** (60–84%) | {len(by_band[CONFIDENCE_MEDIUM])} | Engineer pass per resource (topology shifts: SG/IAM model, etc.) |")
    lines.append(f"| 🔴 **LOW** (<60%) | {len(by_band[CONFIDENCE_LOW])} | Paradigm shifts (IAM bindings, IRSA wiring) — careful design |")
    lines.append(f"| ⚠️ **MANUAL_REVIEW** | {len(by_band[CONFIDENCE_MANUAL])} | No direct AWS equivalent or customer-specific module |")
    lines.append(f"| **Total** | **{total}** | |")
    lines.append("")
    lines.append(f"**Translators registered today: {translated_count}** of {total - len(by_band[CONFIDENCE_MANUAL])} mappable types "
                 f"({round(100 * translated_count / max(1, total - len(by_band[CONFIDENCE_MANUAL])))}%).")
    lines.append("")
    lines.append("✅ = translator registered, emits AWS module body. ⏳ = mapping known, translator pending. 🚫 = no AWS equivalent.")
    lines.append("")

    # Per-band detail tables
    band_headers = {
        CONFIDENCE_HIGH:   "🟢 HIGH confidence",
        CONFIDENCE_MEDIUM: "🟡 MEDIUM confidence",
        CONFIDENCE_LOW:    "🔴 LOW confidence",
        CONFIDENCE_MANUAL: "⚠️ MANUAL_REVIEW",
    }
    for band in (CONFIDENCE_HIGH, CONFIDENCE_MEDIUM, CONFIDENCE_LOW, CONFIDENCE_MANUAL):
        rows = by_band[band]
        if not rows:
            continue
        lines.append(f"## {band_headers[band]} — {len(rows)} resource types")
        lines.append("")
        lines.append("| Status | GCP type | AWS equivalent | Score | Reason |")
        lines.append("|---|---|---|---|---|")
        for tf_type, entry in rows:
            if band == CONFIDENCE_MANUAL:
                status = "🚫"
            elif tf_type in registered_types:
                status = "✅"
            else:
                status = "⏳"
            aws_eq = f"`{entry.aws_equivalent}`" if entry.aws_equivalent else "_(none)_"
            reason = entry.reason.replace("|", "\\|")
            lines.append(f"| {status} | `{tf_type}` | {aws_eq} | {entry.score_pct}% | {reason} |")
        lines.append("")

        # Per-type notes (when present)
        types_with_notes = [(t, e) for t, e in rows if e.notes]
        if types_with_notes:
            lines.append("### Notes / caveats")
            lines.append("")
            for tf_type, entry in types_with_notes:
                lines.append(f"**`{tf_type}`**:")
                for note in entry.notes:
                    lines.append(f"  - {note}")
                lines.append("")

    # Footer
    lines.append("---")
    lines.append("")
    lines.append("## How to extend coverage")
    lines.append("")
    lines.append("1. **Add a mapping entry** in `migrator/plan/coverage.py` `_GCP_TO_AWS` dict.")
    lines.append("2. **Author a translator** at `migrator/translate/<service>.py` exporting `translate()` + `aws_module_spec()`.")
    lines.append("3. **Register it** in `migrator/translate/__init__.py` `TRANSLATORS` dict.")
    lines.append("4. **Re-run this generator** to update this doc:")
    lines.append("   ```")
    lines.append("   python -m migrator.plan.publish_mapping_table")
    lines.append("   ```")
    lines.append("")
    lines.append("See `migrator/translate/customer_profiles/README.md` for adding customer-specific local-ref substitutions without touching engine code.")
    lines.append("")

    return "\n".join(lines)


def write_to_file(path: str) -> None:
    """Write the generated Markdown to disk."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    content = generate_markdown()
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(content)


def _default_output_path() -> str:
    """Default output path: docs/MIGRATOR_COVERAGE.md under repo root."""
    # __file__ is migrator/plan/publish_mapping_table.py
    # Go up two levels to reach repo root, then into docs/.
    here = os.path.dirname(os.path.abspath(__file__))
    repo_root = os.path.dirname(os.path.dirname(here))
    return os.path.join(repo_root, "docs", "MIGRATOR_COVERAGE.md")


if __name__ == "__main__":
    out = _default_output_path()
    write_to_file(out)
    print(f"Wrote coverage doc to: {out}")
