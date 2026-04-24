"""Check that every per-project .terraform.lock.hcl matches the canonical seed.

Why this script exists
----------------------
The canonical lock at ``provider_versions/.terraform.lock.hcl`` is the
single source of truth for provider versions and SHA-256 hashes across
every workdir -- yours, the demo's, and every future SaaS client's.
``common.workdir.seed_lock_file()`` copies this file into each
``imported/<project_id>/`` workdir before ``terraform init`` runs, but
**Terraform itself can mutate the workdir's lock file** in two cases:

    1. ``terraform init -upgrade`` was run (intentional version bump).
    2. A new provider was added to a .tf file that the canonical lock
       did not pin (silent drift -- the workdir gets a new entry, the
       canonical does not).

Case 2 is the dangerous one. A developer could add (say) a
``aws`` provider to support a new resource type, run ``terraform init``
in their workdir, and the canonical lock at the repo root would still
only pin ``google``. Next time a CI run or a fresh client checkout
seeds the canonical into a clean workdir, ``terraform init`` would
resolve a *fresh* aws provider version from the registry -- different
from the one the developer tested.

This script makes that drift visible::

    $ python scripts/check_lock_drift.py
    OK    imported/dev-proj-470211/.terraform.lock.hcl
    OK    imported/prod-470211/.terraform.lock.hcl
    DRIFT imported/test-aws-001/.terraform.lock.hcl
          + provider "registry.terraform.io/hashicorp/aws" only in workdir
          ! canonical at provider_versions/.terraform.lock.hcl needs update

Exit status
-----------
    0 -- all per-project locks match the canonical (or no per-project
         locks exist yet).
    1 -- one or more per-project locks diverge from the canonical.
    2 -- the canonical lock file is missing (cannot compare).

CI integration
--------------
Run as a pre-merge check::

    - name: Check Terraform lock drift
      run: python scripts/check_lock_drift.py

Failing fast in CI prevents the silent-drift class of bug from reaching
the demo or a client deployment.

What this script intentionally does NOT do
------------------------------------------
- It does NOT auto-fix drift. Resolving drift means making a deliberate
  call (was this a legitimate version bump? a new required provider?
  a stale workdir that should be reseeded?) -- the human writing the PR
  is the right person to make that call, not a script.
- It does NOT call ``terraform init`` or talk to the registry. Pure
  file-content comparison; safe to run with no Terraform installed and
  no network access.
- It does NOT consider line-ending differences as drift (Windows CRLF
  vs Unix LF). The lock file is HashiCorp-managed; we normalise both
  files to LF before comparing.

Limitations
-----------
- Diff output is line-based (provider blocks only). For full per-hash
  diffs use ``git diff --no-index`` directly on the two files; this
  script's job is the binary "drift y/n" signal and a human-readable
  hint, not a full diff renderer.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from typing import List, Optional, Set, Tuple

# Repo root = parent of this file's scripts/ directory.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_CANONICAL = os.path.join(_REPO_ROOT, "provider_versions", ".terraform.lock.hcl")
_IMPORTED_BASE = os.path.join(_REPO_ROOT, "imported")

# Matches lines like:  provider "registry.terraform.io/hashicorp/google" {
# Captures the provider source string. Used to enumerate provider blocks
# without parsing the full HCL grammar.
_PROVIDER_LINE_RE = re.compile(r'^\s*provider\s+"([^"]+)"\s*{')


# --------------------------------------------------------------------------- #
# File helpers
# --------------------------------------------------------------------------- #


def _read_normalised(path: str) -> Optional[bytes]:
    """Return file contents with line endings normalised to LF, or None.

    Why normalise: a developer cloning on Windows with autocrlf=true gets
    CRLF in the canonical lock; the workdir's lock (written by terraform
    on Linux/macOS) stays LF. Without normalising, every Windows checkout
    would falsely report drift.
    """
    if not os.path.isfile(path):
        return None
    with open(path, "rb") as f:
        return f.read().replace(b"\r\n", b"\n")


def _extract_providers(contents: bytes) -> Set[str]:
    """Return the set of provider source strings declared in a lock file.

    Used for the "which providers are extra/missing" hint when a drift
    is reported -- much more useful than just "files differ".
    """
    providers: Set[str] = set()
    for line in contents.decode("utf-8", errors="replace").splitlines():
        m = _PROVIDER_LINE_RE.match(line)
        if m:
            providers.add(m.group(1))
    return providers


# --------------------------------------------------------------------------- #
# Drift detection
# --------------------------------------------------------------------------- #


def _find_workdir_locks(base: str) -> List[str]:
    """Return all per-project lock files under ``base``.

    Walks two levels deep to cover both the current single-tenant shape
    (``imported/<project_id>/.terraform.lock.hcl``) and the planned
    multi-tenant shape (``imported/<tenant_id>/<project_id>/.terraform.lock.hcl``).
    Anything deeper is intentionally ignored -- if it shows up we want
    to notice during code review, not silently scan it.
    """
    if not os.path.isdir(base):
        return []

    found: List[str] = []
    for entry in sorted(os.scandir(base), key=lambda e: e.name):
        if not entry.is_dir():
            continue
        # Single-tenant: imported/<project_id>/.terraform.lock.hcl
        single = os.path.join(entry.path, ".terraform.lock.hcl")
        if os.path.isfile(single):
            found.append(single)
        # Multi-tenant: imported/<tenant_id>/<project_id>/.terraform.lock.hcl
        for sub in sorted(os.scandir(entry.path), key=lambda e: e.name):
            if not sub.is_dir():
                continue
            multi = os.path.join(sub.path, ".terraform.lock.hcl")
            if os.path.isfile(multi):
                found.append(multi)
    return found


def _classify(
    canonical_bytes: bytes,
    workdir_path: str,
) -> Tuple[str, List[str]]:
    """Compare a workdir lock to the canonical. Return (status, hints).

    Status values:
        - "OK"    -- byte-identical (after LF normalisation)
        - "DRIFT" -- differs in any way
        - "MISSING" -- workdir file vanished between scan + read (race)

    Hints are human-readable lines explaining the most likely cause
    (extra provider, missing provider, version bump). Empty list when
    the file matches.
    """
    workdir_bytes = _read_normalised(workdir_path)
    if workdir_bytes is None:
        return ("MISSING", [f"vanished during scan: {workdir_path}"])

    if workdir_bytes == canonical_bytes:
        return ("OK", [])

    # Files differ -- categorise the drift to make the report actionable.
    canonical_providers = _extract_providers(canonical_bytes)
    workdir_providers = _extract_providers(workdir_bytes)

    hints: List[str] = []
    extra = workdir_providers - canonical_providers
    missing = canonical_providers - workdir_providers
    common = canonical_providers & workdir_providers

    for p in sorted(extra):
        hints.append(f'+ provider "{p}" only in workdir '
                     f"(canonical needs to add this provider)")
    for p in sorted(missing):
        hints.append(f'- provider "{p}" only in canonical '
                     f"(workdir is stale -- delete it and re-init)")
    if common and not extra and not missing:
        # Same provider set, different bytes -- almost always a version
        # or hash bump. Don't try to parse versions; just say so.
        hints.append("~ same providers but different versions/hashes "
                     "(probably `terraform init -upgrade` was run somewhere)")

    return ("DRIFT", hints)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Check per-project .terraform.lock.hcl files against the "
                    "canonical seed at provider_versions/.terraform.lock.hcl.",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress 'OK' lines; print only drift and the final summary.",
    )
    args = parser.parse_args(argv)

    canonical_bytes = _read_normalised(_CANONICAL)
    if canonical_bytes is None:
        print(f"ERROR canonical lock file not found at {_CANONICAL}",
              file=sys.stderr)
        print("      Cannot check drift without a baseline. Either commit a "
              "canonical lock or run `terraform init` in a clean workdir and "
              "copy its .terraform.lock.hcl to provider_versions/.",
              file=sys.stderr)
        return 2

    workdir_locks = _find_workdir_locks(_IMPORTED_BASE)
    if not workdir_locks:
        # Not an error: a fresh checkout has no per-project workdirs yet.
        print(f"OK    no per-project locks under {_IMPORTED_BASE} -- "
              "nothing to check.")
        return 0

    drift_count = 0
    for lock in workdir_locks:
        status, hints = _classify(canonical_bytes, lock)
        rel = os.path.relpath(lock, _REPO_ROOT)
        if status == "OK":
            if not args.quiet:
                print(f"OK    {rel}")
        else:
            drift_count += 1
            print(f"{status} {rel}")
            for hint in hints:
                print(f"      {hint}")

    print()  # spacer before summary
    if drift_count == 0:
        print(f"All {len(workdir_locks)} per-project lock(s) match the canonical.")
        return 0

    print(f"DRIFT detected in {drift_count} of {len(workdir_locks)} "
          f"per-project lock(s).")
    print("Resolve by either:")
    print("  (a) updating the canonical at provider_versions/.terraform.lock.hcl "
          "to reflect a deliberate version bump or new provider, then re-commit; or")
    print("  (b) deleting the diverged workdir lock(s) and re-running "
          "`terraform init` in those workdirs to re-seed from canonical.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
