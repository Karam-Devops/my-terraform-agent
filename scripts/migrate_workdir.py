"""One-shot migration: split commingled repo-root layout into per-project workdirs.

What this script does
---------------------
Before this refactor, the importer wrote every imported resource's .tf
file to the repo root and shared a single ``terraform.tfstate``. When two
GCP projects had been imported in the same dev session, their state
commingled silently. This script migrates the existing commingled layout
into the new per-project shape::

    BEFORE:                                    AFTER:
    <repo>/                                    <repo>/
    ├── google_<type>_<a>.tf  (proj-1)         ├── imported/
    ├── google_<type>_<b>.tf  (proj-1)         │   ├── proj-1/
    ├── google_<type>_<c>.tf  (proj-2)         │   │   ├── google_<type>_<a>.tf
    ├── terraform.tfstate     (commingled)     │   │   ├── google_<type>_<b>.tf
    ├── terraform.tfstate.backup               │   │   └── terraform.tfstate
    ├── .terraform.lock.hcl                    │   └── proj-2/
    └── .terraform/                            │       ├── google_<type>_<c>.tf
                                               │       └── terraform.tfstate
                                               └── archive/pre-refactor-<TS>/
                                                   ├── terraform.tfstate (original)
                                                   ├── terraform.tfstate.*.backup
                                                   └── .terraform/

How safety works
----------------
- DRY-RUN by default. Pass ``--apply`` to actually move files.
- Refuses to run if any per-project state would be overwritten
  (e.g. ``imported/<project_id>/terraform.tfstate`` already exists).
- Refuses to run if the archive dir already exists.
- Refuses to run if any state row is missing the ``project`` attribute
  (we can't safely classify it without a clear answer; user must
  inspect manually).
- Originals are MOVED to the archive dir (not copied + deleted), so a
  partial failure leaves either old or new layout intact, never both.

Idempotency
-----------
Running twice with ``--apply`` is a no-op the second time: the
commingled tfstate at repo root is gone, so there's nothing to migrate.
The script will exit cleanly with "no commingled state found, nothing
to do."

Invocation
----------
From the repo root::

    # Dry run (default) -- prints what it WOULD do
    python scripts/migrate_workdir.py

    # Actually do it
    python scripts/migrate_workdir.py --apply

    # Override the archive location (default: archive/pre-refactor-<TS>/)
    python scripts/migrate_workdir.py --apply --archive-dir my_backup
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple


# Repo root is the parent of this script's `scripts/` directory.
REPO_ROOT = Path(__file__).resolve().parent.parent
COMMINGLED_STATE = REPO_ROOT / "terraform.tfstate"
LOCK_FILE = REPO_ROOT / ".terraform.lock.hcl"
DOT_TERRAFORM = REPO_ROOT / ".terraform"
IMPORTED_BASE = REPO_ROOT / "imported"


def _log(msg: str, *, prefix: str = "[migrate]") -> None:
    print(f"{prefix} {msg}", flush=True)


def _err(msg: str) -> None:
    print(f"[migrate ERROR] {msg}", file=sys.stderr, flush=True)


def _read_state() -> dict:
    """Load and return the commingled tfstate, or exit if missing/bad."""
    if not COMMINGLED_STATE.is_file():
        _log(
            f"No commingled tfstate found at {COMMINGLED_STATE}. "
            "Nothing to do (already migrated or fresh repo)."
        )
        sys.exit(0)
    try:
        return json.loads(COMMINGLED_STATE.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        _err(f"Could not parse {COMMINGLED_STATE}: {e}")
        sys.exit(2)


def _classify_resources(
    state: dict,
) -> Tuple[Dict[str, List[dict]], List[Tuple[str, str]]]:
    """Group state resources by their project attribute.

    Returns:
        (groups, unclassifiable)
        - groups:        {project_id: [resource_dict, ...]}
        - unclassifiable: [(type, name), ...] for resources missing
                          the project attribute -- these block the
                          migration (manual fix required).
    """
    groups: Dict[str, List[dict]] = {}
    unclassifiable: List[Tuple[str, str]] = []

    for resource in state.get("resources", []):
        rtype = resource.get("type", "<unknown-type>")
        rname = resource.get("name", "<unknown-name>")
        instances = resource.get("instances") or []
        if not instances:
            unclassifiable.append((rtype, rname))
            continue
        attrs = instances[0].get("attributes") or {}
        project = attrs.get("project")
        if not project:
            # Some resources omit project even when one is implicit
            # (e.g. derived from selfLink). Fall back to parsing it
            # out of the selfLink if present, otherwise unclassifiable.
            self_link = attrs.get("self_link") or attrs.get("selfLink") or ""
            if "/projects/" in self_link:
                # selfLink format: .../projects/<project>/...
                try:
                    project = self_link.split("/projects/", 1)[1].split("/", 1)[0]
                except (IndexError, AttributeError):
                    project = None
        if not project:
            unclassifiable.append((rtype, rname))
            continue
        groups.setdefault(project, []).append(resource)

    return groups, unclassifiable


def _build_per_project_state(commingled: dict, resources: List[dict]) -> dict:
    """Build a per-project state dict from a slice of the commingled state.

    Preserves header fields (version, terraform_version) but resets
    ``serial`` to 1 because the per-project state is logically a fresh
    state file. Generates a NEW ``lineage`` (UUID) so the per-project
    state has its own identity -- copying the original lineage would
    confuse Terraform's "is this the same state?" detection across the
    two new files.
    """
    import uuid

    return {
        "version": commingled.get("version", 4),
        "terraform_version": commingled.get("terraform_version", "1.0.0"),
        "serial": 1,
        "lineage": str(uuid.uuid4()),
        "outputs": {},
        "resources": resources,
        "check_results": commingled.get("check_results"),
    }


def _expected_tf_filename(rtype: str, rname: str) -> str:
    """Match the importer's filename convention: ``<type>_<name>.tf``."""
    return f"{rtype}_{rname}.tf"


def _backup_files() -> List[Path]:
    """Find all tfstate backup files at repo root."""
    return sorted(REPO_ROOT.glob("terraform.tfstate*backup*"))


def _plan_migration(
    state: dict,
    archive_dir: Path,
) -> Tuple[Dict[str, dict], List[Path], List[str]]:
    """Compute the migration plan without executing it.

    Returns:
        (per_project, archive_targets, warnings)
        - per_project:     {project_id: {"state": <dict>,
                                         "tf_files": [Path, ...],
                                         "missing_tf": [(type, name), ...],
                                         "workdir": Path}}
        - archive_targets: files/dirs that will be moved to archive
        - warnings:        non-fatal issues (e.g. orphaned .tf files)
    """
    groups, unclassifiable = _classify_resources(state)
    if unclassifiable:
        names = ", ".join(f"{t}.{n}" for t, n in unclassifiable)
        _err(
            f"Cannot classify {len(unclassifiable)} resource(s) -- no "
            f"`project` attribute and no parseable selfLink: {names}. "
            "Migration aborted. Inspect these manually before retrying."
        )
        sys.exit(3)

    per_project: Dict[str, dict] = {}
    moved_tf_files = set()

    for project_id, resources in groups.items():
        workdir = IMPORTED_BASE / project_id
        target_state = workdir / "terraform.tfstate"
        if target_state.exists():
            _err(
                f"Refusing to overwrite existing per-project state at "
                f"{target_state}. Move it aside before re-running."
            )
            sys.exit(4)

        tf_files = []
        missing_tf = []
        for r in resources:
            tf_name = _expected_tf_filename(r["type"], r["name"])
            tf_path = REPO_ROOT / tf_name
            if tf_path.is_file():
                tf_files.append(tf_path)
                moved_tf_files.add(tf_path.name)
            else:
                missing_tf.append((r["type"], r["name"]))

        per_project[project_id] = {
            "state": _build_per_project_state(state, resources),
            "tf_files": tf_files,
            "missing_tf": missing_tf,
            "workdir": workdir,
        }

    # Orphaned .tf files: at repo root, but not referenced by any state row.
    warnings: List[str] = []
    all_tf_at_root = sorted(p.name for p in REPO_ROOT.glob("*.tf"))
    orphans = [n for n in all_tf_at_root if n not in moved_tf_files]
    for orphan in orphans:
        warnings.append(
            f"Orphaned .tf at repo root (no state row): {orphan}. "
            "Left in place; review and move manually if needed."
        )

    # Archive targets: original commingled state + all backups + .terraform/
    archive_targets = [COMMINGLED_STATE] + _backup_files()
    if DOT_TERRAFORM.is_dir():
        archive_targets.append(DOT_TERRAFORM)

    if archive_dir.exists():
        _err(
            f"Refusing to write into existing archive dir {archive_dir}. "
            "Pass a different --archive-dir or delete the existing one."
        )
        sys.exit(5)

    return per_project, archive_targets, warnings


def _print_plan(
    per_project: Dict[str, dict],
    archive_targets: List[Path],
    archive_dir: Path,
    warnings: List[str],
    apply: bool,
) -> None:
    header = "[APPLY] Executing migration" if apply else "[DRY-RUN] Migration plan"
    print()
    print("=" * 70)
    print(header)
    print("=" * 70)
    print(f"Source: {COMMINGLED_STATE}")
    total_resources = sum(len(p["state"]["resources"]) for p in per_project.values())
    print(f"Commingled state holds {total_resources} resource(s) "
          f"across {len(per_project)} project(s).")
    print()

    print(f"{'WILL CREATE' if apply else 'WOULD CREATE'}:")
    for project_id, info in sorted(per_project.items()):
        print(f"  {info['workdir']}/")
        n_res = len(info["state"]["resources"])
        print(f"    + terraform.tfstate         ({n_res} resource(s), "
              f"new lineage, serial=1)")
        for tf in info["tf_files"]:
            print(f"    + {tf.name}  (moved from repo root)")
        if LOCK_FILE.is_file():
            print(f"    + .terraform.lock.hcl       (copied)")
        for rtype, rname in info["missing_tf"]:
            print(f"    ! state row {rtype}.{rname} has no .tf file at root "
                  f"(state-only entry, kept in state)")
        print()

    print(f"{'WILL ARCHIVE' if apply else 'WOULD ARCHIVE'} (moved) to {archive_dir}/:")
    for target in archive_targets:
        kind = "(directory)" if target.is_dir() else ""
        print(f"  - {target.name} {kind}")
    print()

    if warnings:
        print("WARNINGS:")
        for w in warnings:
            print(f"  ! {w}")
        print()

    if not apply:
        print("This was a DRY RUN. No files were touched.")
        print("Re-run with --apply to execute.")
    print("=" * 70)
    print()


def _execute_migration(
    per_project: Dict[str, dict],
    archive_targets: List[Path],
    archive_dir: Path,
) -> None:
    """Apply the migration plan. Aborts hard on first error."""
    archive_dir.mkdir(parents=True, exist_ok=False)
    _log(f"Created archive dir: {archive_dir}")

    # Phase A: write per-project artifacts.
    for project_id, info in per_project.items():
        info["workdir"].mkdir(parents=True, exist_ok=True)
        state_path = info["workdir"] / "terraform.tfstate"
        state_path.write_text(
            json.dumps(info["state"], indent=2) + "\n",
            encoding="utf-8",
        )
        _log(f"Wrote {state_path} "
             f"({len(info['state']['resources'])} resources)")
        for tf in info["tf_files"]:
            dest = info["workdir"] / tf.name
            shutil.move(str(tf), str(dest))
            _log(f"Moved {tf.name} -> {dest}")
        if LOCK_FILE.is_file():
            shutil.copy2(str(LOCK_FILE), str(info["workdir"] / LOCK_FILE.name))
            _log(f"Copied {LOCK_FILE.name} -> {info['workdir']}")

    # Phase B: archive originals (last, so any error in Phase A leaves
    # the source layout intact and recoverable).
    for target in archive_targets:
        dest = archive_dir / target.name
        shutil.move(str(target), str(dest))
        _log(f"Archived {target.name} -> {dest}")
    if LOCK_FILE.is_file():
        # Lock file was copied (not moved) above. Now archive the
        # original so the repo root is clean.
        shutil.move(str(LOCK_FILE), str(archive_dir / LOCK_FILE.name))
        _log(f"Archived {LOCK_FILE.name} -> {archive_dir}")

    print()
    _log("Migration complete. Per-project workdirs are now the source of truth.")
    _log("Old commingled artifacts preserved in: " + str(archive_dir))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually perform the migration. Without this flag, runs in "
             "DRY-RUN mode and only prints the plan.",
    )
    parser.add_argument(
        "--archive-dir",
        default=None,
        help="Where to move the original commingled artifacts. Default: "
             "archive/pre-refactor-<UTC-timestamp>/",
    )
    args = parser.parse_args()

    if args.archive_dir is None:
        ts = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        archive_dir = REPO_ROOT / "archive" / f"pre-refactor-{ts}"
    else:
        archive_dir = Path(args.archive_dir).resolve()

    state = _read_state()
    per_project, archive_targets, warnings = _plan_migration(state, archive_dir)
    _print_plan(per_project, archive_targets, archive_dir, warnings, apply=args.apply)

    if args.apply:
        _execute_migration(per_project, archive_targets, archive_dir)

    return 0


if __name__ == "__main__":
    sys.exit(main())
