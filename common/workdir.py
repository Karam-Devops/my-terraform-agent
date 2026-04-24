# common/workdir.py
"""
Single source of truth for resolving per-project Terraform working directories.

History (why this module exists)
--------------------------------
Before this module, every imported resource's .tf file landed at the repo
root, and every workflow shared a single ``terraform.tfstate``. When two
different GCP projects were imported in the same dev session, their state
commingled silently -- no error at import time, but ``terraform apply``
would fail unpredictably because the GCP provider is project-scoped and
cannot manage two projects from one config block.

Symptom that triggered the fix:
    - importer wrote google_compute_instance_poc_gce.tf  (prod-470211)
    - importer wrote google_compute_disk_poc_disk.tf     (dev-proj-470211)
    - both landed in the same terraform.tfstate at repo root
    - detector ran a per-target plan and deprecation warnings from the
      prod-470211 GKE resource bled into dev-proj-470211 output, masking
      the actual error
    - worse, no easy cleanup: removing a state row for prod-470211 GKE
      could not be done without affecting the prod-470211 lineage

Resolution
----------
Every workflow now resolves a per-project working directory::

    <BASE>/<project_id>/

where ``<BASE>`` is (first hit wins):

    1. ``$MTAGENT_IMPORT_BASE`` env var (deployment override; e.g.
       ``/tmp/imported`` on Cloud Run, or ``gs://my-bucket/imports`` for a
       future remote backend)
    2. ``<repo>/imported/`` (default for local dev)

All artifacts for a project -- .tf files, terraform.tfstate, .terraform/,
.terraform.lock.hcl -- live in this dir. Switching projects switches dirs;
no commingling possible.

SaaS roadmap
------------
This helper accepts a ``tenant_id=None`` parameter that is currently
unused. When we move to multi-tenant Cloud Run, the resolved path becomes::

    <BASE>/<tenant_id>/<project_id>/

which keeps two clients' GCP projects isolated even if they happen to use
the same project_id (common in practice -- every client has a "prod-..."
project). Adding the tenant layer is a one-line change to the resolver,
not a refactor of the consumers.

Storage abstraction (also part of the SaaS roadmap):

    - today: local FS only
    - next: ``$MTAGENT_IMPORT_BASE`` may be an absolute path on Cloud
      Run's ephemeral /tmp. This works because Terraform state can be a
      remote backend declared in the .tf files; the working dir just
      needs to be writeable for ``terraform init`` to drop .terraform/.
    - later: ``$MTAGENT_IMPORT_BASE`` may be a ``gs://`` URI if we
      abstract file IO through a storage layer. Not implemented today,
      but the env-var contract is forward-compatible: callers do not
      assume the base is on local disk.

Path-traversal safety
---------------------
Both ``project_id`` and ``tenant_id`` are validated against strict regexes
before being joined into a path. This prevents a malicious or buggy
caller from passing ``"../etc/passwd"`` or similar and escaping the base
dir. Invalid IDs raise ``ValueError`` with a clear message.

The project_id regex matches GCP's documented project ID format
(lowercase letters, digits, hyphens; 6-30 chars; starts with a letter,
ends with letter or digit). If we ever add non-GCP imports, this regex
will need to become cloud-aware.

Caching
-------
The resolved base dir is cached process-wide after the first lookup
(mirrors ``terraform_path.py``). Per-project subdirs are NOT cached --
they are cheap to recompute and we WANT to honour env-var changes
mid-test. Use ``reset_cache()`` to invalidate the base cache (intended
for tests).
"""

from __future__ import annotations

import os
import re
import shutil
from typing import List, Optional

# Default base, relative to repo root (not absolute, so tests can override
# behaviour by patching the env var without touching the filesystem layout).
_DEFAULT_BASE_RELATIVE = "imported"

# Canonical Terraform provider lock file. One committed file at the repo
# root, copied (seeded) into every freshly-created per-project workdir
# before `terraform init` runs. See `seed_lock_file()` for rationale.
_PROVIDER_LOCK_RELATIVE = os.path.join("provider_versions", ".terraform.lock.hcl")
_PROVIDER_LOCK_FILENAME = ".terraform.lock.hcl"

# Cached absolute base dir. Reset via reset_cache().
_cached_base: Optional[str] = None

# GCP project IDs: lowercase letters, digits, hyphens; 6-30 chars; starts
# with a letter, ends with letter or digit. Format is documented at
# https://cloud.google.com/resource-manager/reference/rest/v1/projects.
# Strict match prevents path traversal via "../foo" or absolute paths.
_PROJECT_ID_RE = re.compile(r"^[a-z][-a-z0-9]{4,28}[a-z0-9]$")

# Tenant IDs (reserved for future multi-tenant SaaS use). Permissive for
# now -- alphanumeric, hyphens, underscores, max 63 chars. Tighten when
# the actual tenant scheme is decided (likely UUID or short slug).
_TENANT_ID_RE = re.compile(r"^[a-zA-Z0-9][-a-zA-Z0-9_]{0,62}$")


def _repo_root() -> str:
    """Repo root = parent of this file's ``common/`` directory."""
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _resolve_base() -> str:
    """Resolve and cache the import base directory.

    Honours ``$MTAGENT_IMPORT_BASE`` if set; absolute values are used
    as-is (Cloud Run pattern), relative values resolve against repo root
    for predictability.
    """
    global _cached_base
    if _cached_base:
        return _cached_base

    env_override = os.environ.get("MTAGENT_IMPORT_BASE")
    if env_override:
        if os.path.isabs(env_override):
            _cached_base = env_override
        else:
            _cached_base = os.path.normpath(
                os.path.join(_repo_root(), env_override)
            )
    else:
        _cached_base = os.path.join(_repo_root(), _DEFAULT_BASE_RELATIVE)

    return _cached_base


def resolve_project_workdir(
    project_id: str,
    *,
    tenant_id: Optional[str] = None,
    create: bool = True,
) -> str:
    """Return the absolute path to the working dir for a (tenant, project).

    Args:
        project_id: GCP project ID. Must match GCP's documented format
            (lowercase, digits, hyphens; 6-30 chars; starts with letter,
            ends with letter or digit). Rejected with ValueError if
            invalid -- this is a path-traversal guard, not just hygiene.
        tenant_id: Reserved for future multi-tenant SaaS deployments.
            Currently unused; pass ``None``. When provided, the resolved
            path becomes ``<base>/<tenant_id>/<project_id>/`` to keep
            different clients' GCP projects isolated even if their
            project_ids collide.
        create: If True (default), create the directory if it does not
            exist. Tests sometimes pass ``create=False`` to assert the
            resolver does not accidentally create dirs as a side effect
            of a path-only operation.

    Returns:
        Absolute path to the working dir. Example::

            C:\\\\Users\\\\41708\\\\my-terraform-agent\\\\imported\\\\dev-proj-470211

    Raises:
        ValueError: ``project_id`` or ``tenant_id`` contains invalid
            characters (path traversal protection).
        OSError: Directory creation failed (permissions, disk full, etc.)
            -- propagated from ``os.makedirs``.
    """
    if not _PROJECT_ID_RE.match(project_id):
        raise ValueError(
            f"Invalid project_id {project_id!r}: must match GCP project "
            f"ID format (lowercase letters, digits, hyphens; 6-30 chars; "
            f"starts with a letter, ends with letter or digit)."
        )

    base = _resolve_base()
    if tenant_id is not None:
        if not _TENANT_ID_RE.match(tenant_id):
            raise ValueError(
                f"Invalid tenant_id {tenant_id!r}: must be alphanumeric "
                f"with hyphens or underscores, max 63 chars."
            )
        workdir = os.path.join(base, tenant_id, project_id)
    else:
        workdir = os.path.join(base, project_id)

    if create:
        os.makedirs(workdir, exist_ok=True)

    return workdir


def canonical_lock_file_path() -> str:
    """Absolute path to the committed canonical Terraform lock file.

    There is exactly one ``.terraform.lock.hcl`` in the repo, at
    ``provider_versions/.terraform.lock.hcl``. Every per-project workdir
    is seeded from this file via :func:`seed_lock_file` before
    ``terraform init`` runs, so all projects -- yours, the demo's, and
    every future SaaS client's -- resolve provider versions and SHA-256
    hashes identically.

    Why centralised: the per-project lock files cannot be committed in a
    SaaS context (clients bring their own ``project_id`` values, so a
    repo-committed ``imported/<project_id>/.terraform.lock.hcl`` would
    only ever apply to the developer's own POC projects). Centralising
    the canonical lock means the version pin lives in source control
    once, and is transparently re-applied to every workdir at init time.
    """
    return os.path.join(_repo_root(), _PROVIDER_LOCK_RELATIVE)


def seed_lock_file(workdir: str) -> bool:
    """Copy the canonical ``.terraform.lock.hcl`` into ``workdir`` if absent.

    Called by ``importer.terraform_client.init()`` before ``terraform
    init`` runs. The behaviour is intentionally conservative:

      * If ``workdir/.terraform.lock.hcl`` already exists -> no-op
        (the operator's existing lock wins; we never silently overwrite
        a workdir's pinned versions).
      * If the canonical seed at ``provider_versions/.terraform.lock.hcl``
        does not exist -> no-op (Terraform will create a fresh lock from
        the registry, same as a clean ``terraform init``).
      * Otherwise, copy the seed into ``workdir``.

    Args:
        workdir: Absolute path to a per-project working directory. Must
            already exist (caller's responsibility, typically via
            :func:`resolve_project_workdir` with ``create=True``).

    Returns:
        ``True`` if the seed was copied; ``False`` if the seed was a
        no-op (already present, or no canonical seed available).

    Raises:
        OSError: Copy failed (permissions, disk full). Propagated so the
            init path fails fast rather than silently running without a
            seeded lock.
    """
    target = os.path.join(workdir, _PROVIDER_LOCK_FILENAME)
    if os.path.isfile(target):
        return False
    source = canonical_lock_file_path()
    if not os.path.isfile(source):
        return False
    shutil.copy2(source, target)
    return True


def list_project_workdirs(
    *, tenant_id: Optional[str] = None
) -> List[str]:
    """Return all existing project_ids under the resolved base.

    Used by detector/remediator CLIs when no ``--project`` flag is
    given -- they show a menu of available projects from existing
    workdirs.

    Args:
        tenant_id: Reserved for future multi-tenant use; pass ``None``
            today.

    Returns:
        Sorted list of project_id strings (bare names, NOT full paths).
        Empty if the base dir does not exist or contains no valid
        project subdirs.
    """
    base = _resolve_base()
    if tenant_id is not None:
        if not _TENANT_ID_RE.match(tenant_id):
            raise ValueError(f"Invalid tenant_id {tenant_id!r}")
        scan_dir = os.path.join(base, tenant_id)
    else:
        scan_dir = base

    if not os.path.isdir(scan_dir):
        return []

    return sorted(
        entry.name
        for entry in os.scandir(scan_dir)
        if entry.is_dir() and _PROJECT_ID_RE.match(entry.name)
    )


def reset_cache() -> None:
    """Clear the cached base dir resolution. Intended for tests."""
    global _cached_base
    _cached_base = None
