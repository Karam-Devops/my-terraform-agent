# importer/golden_examples_loader.py
"""CC-9 few-shot golden example loader.

Loads hand-written, plan-clean HCL examples from
``importer/golden_examples/`` and returns them in a form ready to
prepend to the LLM system prompt as a "REFERENCE EXAMPLE" section.

Filename convention:
  * ``<tf_type>.tf`` -- default example for the type.
  * ``<tf_type>__<mode_id>.tf`` -- mode-specialized variant (mode IDs
    come from ``importer.resource_mode.detect_modes()`` -- e.g.
    ``gke_autopilot``, ``gke_standard``).

Lookup order:
  1. For each mode in ``modes`` (if any), try
     ``<tf_type>__<mode>.tf``. First hit wins.
  2. Fall back to ``<tf_type>.tf``.
  3. Return None if neither exists (no example to inject).

Pure function -- no I/O beyond the file read; no caching (tradeoff:
adds <1ms per call but keeps tests deterministic and avoids
stale-file gotchas during development. Cache later if profiling
shows it matters).
"""

from __future__ import annotations

import os
from typing import Iterable, Optional


_GOLDEN_DIR = os.path.join(os.path.dirname(__file__), "golden_examples")


def load_golden_example(
    tf_type: str,
    modes: Optional[Iterable[str]] = None,
) -> Optional[str]:
    """Return the golden example HCL for the given tf_type, or None.

    Args:
        tf_type: e.g. "google_container_cluster".
        modes: Optional iterable of mode IDs from
            ``resource_mode.detect_modes(cloud_data, tf_type)``.
            Mode-specialized files are tried first in iteration
            order.

    Returns:
        The HCL file contents (string), or None if no matching
        example exists. Caller decides whether to inject (typically
        always inject when present).
    """
    if not tf_type:
        return None

    candidates: list[str] = []
    if modes:
        for mode in modes:
            if not mode:
                continue
            candidates.append(f"{tf_type}__{mode}.tf")
    candidates.append(f"{tf_type}.tf")

    for fname in candidates:
        path = os.path.join(_GOLDEN_DIR, fname)
        if os.path.isfile(path):
            with open(path, "r", encoding="utf-8") as f:
                return f.read()

    return None


def format_example_section(example_hcl: str) -> str:
    """Wrap a golden example in the prompt-injection markers.

    Conventions chosen for the system prompt:
      * Clearly-marked section header so the LLM doesn't mistake
        the example for the input it should regenerate.
      * Explicit instructional language ("Use this as a REFERENCE
        for shape; do NOT copy literal values").
      * Trailing newline so adjacent prompt sections don't run
        together.

    Args:
        example_hcl: Raw HCL content from load_golden_example.

    Returns:
        A formatted string ready to concatenate into system_prompt.
    """
    return (
        "\n\n--- REFERENCE EXAMPLE (HCL shape, NOT to copy verbatim) ---\n"
        "Below is a hand-verified, plan-clean example of the canonical\n"
        "shape for this resource type. USE IT AS A SHAPE REFERENCE:\n"
        "  * Mirror the field names, nesting, and value types shown.\n"
        "  * Mirror the ABSENCE of fields shown -- if the example\n"
        "    omits a field, that field is either v1-vestige,\n"
        "    Autopilot-managed, computed-only, or otherwise wrong\n"
        "    on this shape.\n"
        "  * Replace literal values (names, project IDs, regions,\n"
        "    image refs) with the corresponding values from the\n"
        "    INPUT JSON below.\n"
        "  * Replace the resource's local label with the requested\n"
        "    `hcl_name` from the TASK section.\n"
        "DO NOT include any of the example's literal identifiers in\n"
        "your output -- copy SHAPE only, not values.\n"
        "\n"
        f"{example_hcl}\n"
        "--- END REFERENCE EXAMPLE ---\n"
    )
