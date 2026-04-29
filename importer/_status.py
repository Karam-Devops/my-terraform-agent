# importer/_status.py
"""PUI-1F: workdir status helpers (Firefly/ControlMonkey "Codified" parity).

Single source of truth for two questions both the UI AND the engine need
to answer to avoid burning LLM calls on already-imported resources:

  1. "What .tf filename WILL this discovered resource be saved under?"
     -> ``expected_tf_filename(tf_type, asset_type, cloud_name)``

  2. "Given a discovered resource, what's its current import status?"
     -> ``classify_status(filename, imported_set, quarantined_set)``

Why one helper module instead of duplicating the heuristic in two places:
the filename derivation has subtle exceptions (service accounts use the
local part of the email; URN-style displayNames collapse to the last
segment via friendly_name_from_display upstream). A UI-side mismatch
with the engine's _map_asset_to_terraform would let a "looks already
imported" row slip through the engine guard and burn an LLM call anyway
-- exactly the bug class PUI-1F is trying to eliminate.

By extracting the derivation here:
  * The UI imports ``expected_tf_filename`` to render the Status column.
  * The engine (importer.run.run_workflow) imports the same function
    to compute the engine guard's skip-set.
  * Any future refactor of the filename heuristic touches one place.

Pure functions; no I/O. Imported by both Streamlit pages (cheap) and
engine code (no test fixtures needed).
"""

from __future__ import annotations

from typing import Iterable, Optional


# tf_types that always go through the heavyweight Pro model (PERF-T3b).
# These are the resources whose schemas + interdependencies (mutex pairs,
# nested blocks, cross-resource references) overwhelm Flash's schema-
# adherence quality. Verified empirically on smoke 2026-04-29.
#
# Lives next to the status helpers because the same module is already
# imported by the engine + UI, so adding a "is this a complex resource"
# accessor here avoids creating yet another shared import seam.
_COMPLEX_TF_TYPES = frozenset({
    "google_container_cluster",
    "google_container_node_pool",
})


def is_complex_tf_type(tf_type: str) -> bool:
    """Return True iff ``tf_type`` is in the set that REQUIRES Gemini Pro
    (vs the Flash default for everything else).

    Used by importer.hcl_generator to pick the model client. The set is
    deliberately small: most Google resources have flat-ish schemas
    that Flash handles fine, so the default is Flash and the override
    is "use Pro for these complex types." Adding a new entry here is
    the right move when we observe a pattern of Flash producing
    quarantine-worthy HCL on a specific resource type.
    """
    return tf_type in _COMPLEX_TF_TYPES


def expected_tf_filename(
    tf_type: str,
    asset_type: str,
    cloud_name: str,
) -> Optional[str]:
    """Compute the .tf filename a resource will be saved under by the
    importer's ``_map_asset_to_terraform`` (importer/run.py).

    Mirrors that function's hcl_name_base derivation:
      * Service accounts: local part of the email (split on '@'). The
        cloud_name we receive here is already the email (the IAM-SDK
        listing path populates displayName=sa.email, and
        friendly_name_from_display preserves emails since they have no
        '/' separator). So we just split off the local part.
      * All other types: cloud_name is already the friendly short name
        thanks to friendly_name_from_display upstream -- we use it as
        the hcl_name_base directly.

    Then we snake-case hyphens (HCL identifiers can't contain '-') and
    prefix with tf_type, matching the engine's filename format
    ``f"{tf_type}_{hcl_name_base.replace('-', '_')}.tf"``.

    Returns:
        The expected filename string. ``None`` if cloud_name is empty
        (caller should treat as "not imported" -- can't match a missing
        identifier; the engine would have skipped this resource at the
        mapping stage too).

    Why we don't just import _map_asset_to_terraform: it does much more
    work (resolves import_id_format, looks up parent identifiers,
    builds the full mapping dict). This helper is the FILENAME-only
    slice -- enough for status lookup, no engine state needed.
    """
    if not cloud_name:
        return None
    if asset_type == "iam.googleapis.com/ServiceAccount":
        # cloud_name == sa.email here (PUI-1B v3.1 IAM-SDK listing path).
        # The .tf filename uses just the local part so the filename is
        # always HCL-identifier-safe (no '@' or '.').
        hcl_name_base = cloud_name.split("@", 1)[0]
    else:
        hcl_name_base = cloud_name
    hcl_name_base = hcl_name_base.replace("-", "_")
    return f"{tf_type}_{hcl_name_base}.tf"


def classify_status(
    filename: Optional[str],
    *,
    imported_set: Iterable[str],
    quarantined_set: Iterable[str],
) -> str:
    """Return one of ``"imported"``, ``"needs_attention"``, ``"none"``
    given a filename and the two membership sets.

    Both sets are pre-built by the caller (UI from
    ``list_workdir_tf_files`` results; engine from a local workdir
    scan). Iterables (not strict sets) so callers can pass list/set/
    dict-keys interchangeably.

    Args:
        filename: The expected .tf filename for the resource (output
            of ``expected_tf_filename``). ``None`` -> "none".
        imported_set: Filenames present at the workdir top level
            (passed terraform plan verification).
        quarantined_set: Filenames present under workdir/_quarantine/
            (failed plan verification; needs operator review).

    Returns:
        One of:
          * ``"imported"``        -- in imported_set
          * ``"needs_attention"`` -- in quarantined_set
          * ``"none"``            -- in neither (or filename is None)

    String returns (not an enum) keep this trivially serialisable into
    Streamlit's session_state and JSON-loggable as-is.
    """
    if not filename:
        return "none"
    # imported takes precedence over needs_attention -- if a resource
    # was successfully re-imported after a quarantine, the top-level .tf
    # is the source of truth. (Engine cleans the quarantine sidecar on
    # successful re-import; defensive even if it didn't.)
    if filename in imported_set:
        return "imported"
    if filename in quarantined_set:
        return "needs_attention"
    return "none"
