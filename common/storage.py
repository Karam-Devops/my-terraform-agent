# common/storage.py
"""GCS-backed per-project workdir hydration + persistence (CG-8H).

Phase 5A PSA-3. Bridges the importer/translator/detector engines (all
of which read/write under ``common.workdir.resolve_project_workdir``)
and the Cloud Run deployment's ephemeral ``/tmp`` filesystem, by
syncing the per-project workdir to a versioned GCS bucket at request
boundaries.

Shape per the CG-8H spec in ``docs/saas_readiness_punchlist.md``:

    Cloud Run handler                                     GCS state bucket
    ┌────────────────────────┐         hydrate          ┌─────────────────┐
    │ /tmp/imported/         │ ◄────────────────────── │ gs://...state/  │
    │   <request_uuid>/      │                          │  tenants/<t>/   │
    │     dev-proj-470211/   │  (engine reads + writes  │   projects/<p>/ │
    │       *.tf             │   here for the duration  │     *.tf        │
    │       terraform.tfstate│   of the request)        │     *.tfstate   │
    │       _quarantine/     │                          │     _quarantine/│
    └────────────────────────┘         persist          └─────────────────┘
                              ────────────────────────►

Per-request lifecycle (called from PSA-4's request middleware):

    1. ``request_uuid = uuid4()``
    2. ``local = hydrate_workdir(tenant_id, project_id)``
       Returns ``/tmp/imported/<request_uuid>/dev-proj-470211``.
       The ``MTAGENT_IMPORT_BASE`` env var is set globally to
       ``/tmp/imported/<request_uuid>/`` so the engines that use
       ``common.workdir.resolve_project_workdir`` find the hydrated
       state automatically -- no engine code changes needed.
    3. Engine code runs against the local path.
    4. ``persist_workdir(local, tenant_id, project_id)`` syncs back
       to GCS, excluding ephemeral diagnostics + backup files.
    5. The request middleware ``rm -rf /tmp/imported/<request_uuid>/``
       to free disk space.

Design choices:

* **gcloud storage rsync** (not ``gsutil cp -r``): rsync skips
  unchanged files, so a "translate one more file" request after a
  full importer run only uploads the new ``.tf``, not the entire
  ``.terraform/providers/`` blob (which is ~150MB). 10x speedup
  on incremental writes.

* **GCS object versioning** (set ON in the bucket per the bootstrap
  script): every persist call creates a new generation of each
  changed file. Operator can roll back any object via
  ``gcloud storage objects restore``. Storage cost is ~free for
  Round-1 scale (KB-scale state files).

* **Per-tenant subdirectory under a SINGLE bucket** (not bucket-per-
  tenant): one bucket has one IAM policy, one lifecycle config, one
  audit configuration. The runtime SA's ``storage.objectAdmin`` is
  scoped to this bucket only (granted in
  ``scripts/bootstrap_host_project.sh`` step 6) so cross-tenant data
  access requires bypassing GCP IAM, not just guessing a bucket name.

* **Excluded from persist**: ``_diagnostics/**`` (translator YAML
  blueprints; ephemeral debug-only) and ``*.backup`` (terraform
  auto-backup files; redundant given object versioning). Reduces
  GCS write volume + keeps the bucket focused on customer-relevant
  artifacts.

Test coverage: ``common/tests/test_storage.py`` (mocked subprocess.run;
no real GCS calls in unit tests). One integration test marked
``@unittest.skipUnless(os.environ.get("MTAGENT_INTEGRATION"))`` that
round-trips against a real bucket -- run manually before each
release.
"""

from __future__ import annotations

import os
import re
import subprocess
from typing import List, Optional

from common.logging import get_logger

_log = get_logger(__name__)

# Default bucket name. Overridable via MTAGENT_STATE_BUCKET env var
# (set in cloudbuild.yaml deploy step). Sensible Stage-1 default.
_DEFAULT_BUCKET = "mtagent-state-dev"

# Default tenant ID for single-tenant deployments. Multi-tenant SaaS
# (CG-9 / Phase 6+) replaces this with a real tenant_id from the
# IAP token / session. Until then "default" gives us the per-tenant
# directory structure for free, ready for the future split.
_DEFAULT_TENANT_ID = "default"

# Tenant ID validation: same shape as common/workdir._TENANT_ID_RE.
# Strict pattern prevents path traversal via gs://bucket/../foo
# tricks even though gcloud storage would reject those too.
_TENANT_ID_RE = re.compile(r"^[a-zA-Z0-9][-a-zA-Z0-9_]{0,62}$")

# GCP project IDs (same regex as common/workdir).
_PROJECT_ID_RE = re.compile(r"^[a-z][-a-z0-9]{4,28}[a-z0-9]$")

# Files / directories never persisted to GCS:
#   - _diagnostics/**: translator's YAML blueprint dump
#     (MTAGENT_PERSIST_BLUEPRINTS=0 disables it anyway in SaaS mode,
#     but excluding here is belt-and-braces)
#   - *.backup: terraform's auto-backup files (redundant given GCS
#     object versioning); also *.tfstate.backup variants
#   - terraform.tfstate / .tfstate*: with PSA-5's GCS backend, state
#     lives directly in gs://bucket/.../default.tfstate (managed by
#     terraform itself with native object-locking). Local
#     terraform.tfstate should never exist after init -- but if
#     something legacy leaves one around, we MUST NOT rsync it back
#     to GCS, because that would clobber whatever terraform has
#     written to the canonical backend path.
#   - terraform.tfstate.lock.info: terraform's local lock file (if
#     any); always ephemeral
_PERSIST_EXCLUDES = (
    "_diagnostics/**",
    "*.backup",
    "*.tfstate.backup",
    "*.tfstate.*.backup",
    "terraform.tfstate",
    "terraform.tfstate.lock.info",
)


# --- PSA-5: Terraform GCS backend wiring ----------------------------
#
# When the importer creates a fresh per-project workdir, alongside the
# seeded lock file + providers stub (D-6 fix), it ALSO writes a tiny
# `_backend_seed.tf` that declares::
#
#     terraform {
#       backend "gcs" {
#         bucket = "<MTAGENT_STATE_BUCKET>"
#         prefix = "tenants/<tenant>/projects/<project>/terraform-state"
#       }
#     }
#
# `terraform init` then initializes state against this GCS path
# directly (no local terraform.tfstate) -- which buys us:
#
#   * Native object-level locking via GCS preconditions (no app-side
#     coordination needed for concurrent Cloud Run instances)
#   * Atomic writes (concurrent reads never see partial state)
#   * GCS versioning + bucket lock = full audit trail of every state
#     mutation, with operator-grade rollback
#
# The `prefix` MIRRORS PSA-3's _gcs_prefix shape but appends
# "terraform-state" so the state object lives BENEATH the .tf-files
# subtree (gcloud rsync still uploads the .tf siblings, terraform owns
# the state subdir). Single bucket, separate paths, no collision.

_BACKEND_SEED_FILENAME = "_backend_seed.tf"


def _state_prefix(tenant_id: str, project_id: str) -> str:
    """Return the GCS prefix for the terraform-state subtree.

    Pattern: ``tenants/<tenant_id>/projects/<project_id>/terraform-state``

    Note: NO leading or trailing slash -- this is the format the
    terraform GCS backend's ``prefix`` field expects (terraform appends
    ``/default.tfstate`` itself).
    """
    return f"tenants/{tenant_id}/projects/{project_id}/terraform-state"


def generate_backend_config(
    project_id: str,
    *,
    tenant_id: Optional[str] = None,
) -> str:
    """Return the HCL block declaring the terraform GCS backend.

    Output is a complete `_backend_seed.tf` content -- callers can
    write the return value directly to disk via ``seed_backend_config``
    (recommended) or embed it elsewhere if they have a different
    delivery mechanism.

    Args:
        project_id: GCP project ID being scanned.
        tenant_id: Multi-tenant identifier (defaults to "default").

    Returns:
        HCL string. Example::

            terraform {
              backend "gcs" {
                bucket = "mtagent-state-dev"
                prefix = "tenants/default/projects/dev-proj-470211/terraform-state"
              }
            }
    """
    tenant = tenant_id or _DEFAULT_TENANT_ID
    _validate_ids(tenant, project_id)
    bucket = state_bucket()
    prefix = _state_prefix(tenant, project_id)
    return (
        "# AUTO-GENERATED by common.storage.seed_backend_config (PSA-5).\n"
        "# Do not edit -- regenerated when the per-project workdir is\n"
        "# bootstrapped by the importer. terraform init reads this\n"
        "# block to wire state I/O directly to GCS, with native\n"
        "# object-level locking + bucket versioning for audit trail.\n"
        "#\n"
        "# To migrate from local-state to GCS-state on an existing\n"
        "# workdir: delete terraform.tfstate, terraform.tfstate.backup,\n"
        "# and the .terraform/terraform.tfstate metadata, then re-run\n"
        "# `terraform init` -- terraform will prompt to migrate.\n"
        "\n"
        "terraform {\n"
        '  backend "gcs" {\n'
        f'    bucket = "{bucket}"\n'
        f'    prefix = "{prefix}"\n'
        "  }\n"
        "}\n"
    )


def gcs_backend_enabled() -> bool:
    """Return True iff the GCS backend should be wired into new workdirs.

    Gated on the ``MTAGENT_USE_GCS_BACKEND`` env var:
      * Set to ``"1"`` (or ``"true"`` / ``"yes"`` / ``"on"``) -> True
      * Anything else (including unset) -> False

    Default OFF preserves local-dev behaviour (terraform.tfstate
    stays local; no GCS auth needed). Cloud Run cloudbuild.yaml sets
    the env to ``"1"`` so production deploys use the backend.
    """
    raw = os.environ.get("MTAGENT_USE_GCS_BACKEND", "").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def seed_backend_config(
    workdir: str,
    project_id: str,
    *,
    tenant_id: Optional[str] = None,
) -> bool:
    """Write the GCS backend HCL into ``workdir/_backend_seed.tf``.

    Idempotent in the same shape as ``common.workdir.seed_lock_file``
    + ``seed_providers_stub`` (D-6 fix infrastructure):

      * If ``MTAGENT_USE_GCS_BACKEND`` is unset / off -> no-op.
        Preserves local-dev behaviour (no GCS auth required for
        ``terraform init``).
      * If the workdir already has a backend config (operator-edited
        OR previously seeded), no-op -- never silently overwrite an
        operator's customization.
      * Otherwise, generate the canonical config and write it.

    Args:
        workdir: Absolute path to a per-project working directory.
        project_id: GCP project ID being scanned.
        tenant_id: Multi-tenant identifier (defaults to "default").

    Returns:
        ``True`` if a new file was written; ``False`` for any of the
        no-op cases (env disabled, file already exists).

    Raises:
        ValueError: project_id or tenant_id failed validation (only
            when env is enabled and we'd actually write).
        OSError: write failed (permissions, disk full).
    """
    if not gcs_backend_enabled():
        return False
    target = os.path.join(workdir, _BACKEND_SEED_FILENAME)
    if os.path.isfile(target):
        return False
    content = generate_backend_config(project_id, tenant_id=tenant_id)
    with open(target, "w", encoding="utf-8") as fh:
        fh.write(content)
    return True


def state_bucket() -> str:
    """Return the GCS bucket name for state persistence.

    Reads ``MTAGENT_STATE_BUCKET`` env var first (production / Cloud
    Run config), falls back to the Stage-1 default ``mtagent-state-dev``.
    Single source of truth -- callers must NOT read the env var
    directly so the default chain stays in one place.
    """
    return os.environ.get("MTAGENT_STATE_BUCKET", _DEFAULT_BUCKET)


def _gcs_prefix(tenant_id: str, project_id: str) -> str:
    """Return the canonical gs:// URI for a (tenant, project) pair.

    Layout::

        gs://<bucket>/tenants/<tenant_id>/projects/<project_id>/

    The trailing slash is significant: ``gcloud storage rsync`` treats
    a slash-suffixed URI as "sync the directory contents" rather than
    "sync the directory itself as a child of the destination."
    """
    bucket = state_bucket()
    return f"gs://{bucket}/tenants/{tenant_id}/projects/{project_id}/"


def _validate_ids(tenant_id: str, project_id: str) -> None:
    """Reject malformed tenant_id / project_id before they reach gcloud.

    Path-traversal guard. Same regex as common/workdir applies; we
    re-validate here because storage.py callers may bypass workdir.
    """
    if not _TENANT_ID_RE.match(tenant_id):
        raise ValueError(
            f"Invalid tenant_id {tenant_id!r}: must match "
            f"alphanumeric + hyphens/underscores, 1-63 chars, starts "
            f"with alphanumeric"
        )
    if not _PROJECT_ID_RE.match(project_id):
        raise ValueError(
            f"Invalid project_id {project_id!r}: must match GCP project "
            f"ID format (lowercase, digits, hyphens; 6-30 chars; "
            f"starts with letter, ends with letter or digit)"
        )


def _run_gcloud(args: List[str]) -> subprocess.CompletedProcess:
    """Run a gcloud command, raising CalledProcessError on non-zero exit.

    Captures stdout/stderr so the caller can include them in error
    messages. Uses ``check=True`` so the caller doesn't have to
    inspect ``.returncode`` -- failures bubble up as exceptions.

    Pulled into a helper so tests can patch a single seam:
    ``patch("common.storage._run_gcloud")``.
    """
    return subprocess.run(
        args,
        check=True,
        capture_output=True,
        text=True,
    )


def hydrate_workdir(
    project_id: str,
    *,
    tenant_id: Optional[str] = None,
    local_root: Optional[str] = None,
) -> str:
    """Pull the (tenant, project) state from GCS into a local workdir.

    Called at the start of a Cloud Run request. Engine code that runs
    after this point sees the same on-disk shape it would see on a
    bare-metal dev machine (.tf files, terraform.tfstate,
    .terraform/, _quarantine/), populated from the GCS persist of
    the previous request (or empty for a brand-new project).

    Args:
        project_id: GCP project ID being scanned (e.g.
            ``dev-proj-470211``). Strict-validated against the GCP
            project ID format to prevent path traversal.
        tenant_id: Multi-tenant identifier. Defaults to ``"default"``
            for Round-1 single-tenant deployments. CG-9 / Phase 6+
            wires this from the IAP token.
        local_root: Override the local destination root. Defaults to
            ``$MTAGENT_IMPORT_BASE`` (set by Cloud Run env;
            typically ``/tmp/imported/<request_uuid>/``). Tests pass
            an explicit tmp path.

    Returns:
        Absolute local path to the per-project workdir.

    Raises:
        ValueError: tenant_id or project_id failed validation.
        subprocess.CalledProcessError: ``gcloud storage rsync``
            failed (network, perms, bucket missing). Caller decides
            whether to retry or surface the error.
        OSError: local directory creation failed.
    """
    tenant = tenant_id or _DEFAULT_TENANT_ID
    _validate_ids(tenant, project_id)

    base = local_root or os.environ.get("MTAGENT_IMPORT_BASE", "/tmp/imported")
    local_path = os.path.join(base, project_id)
    os.makedirs(local_path, exist_ok=True)

    src = _gcs_prefix(tenant, project_id)

    _log.info(
        "storage_hydrate_start",
        tenant_id=tenant,
        project_id=project_id,
        src=src,
        local_path=local_path,
    )

    # gcloud storage rsync semantics:
    #   --recursive: walk subdirectories
    #   No --delete: hydrate is additive (we never delete local files
    #     based on what's in GCS). The /tmp dir is fresh per request
    #     anyway, so additive == authoritative-from-GCS in practice.
    #   We don't pass --exclude on hydrate (we want everything that
    #     was persisted, including state files, locks, .terraform/).
    try:
        _run_gcloud([
            "gcloud", "storage", "rsync",
            "--recursive",
            src,
            local_path,
        ])
    except subprocess.CalledProcessError as e:
        _log.error(
            "storage_hydrate_failed",
            tenant_id=tenant,
            project_id=project_id,
            src=src,
            local_path=local_path,
            stderr=(e.stderr or "")[:500],
        )
        raise

    _log.info(
        "storage_hydrate_complete",
        tenant_id=tenant,
        project_id=project_id,
        local_path=local_path,
    )
    return local_path


def persist_workdir(
    local_path: str,
    project_id: str,
    *,
    tenant_id: Optional[str] = None,
) -> None:
    """Push the local workdir back to GCS at the end of a request.

    Excludes ephemeral artifacts (``_diagnostics/``, ``*.backup``)
    that don't need cross-session persistence. GCS object versioning
    (enabled by the bootstrap script) creates a new generation for
    every changed file, so the upload is fully reversible.

    Args:
        local_path: The workdir that was returned by
            ``hydrate_workdir``. Caller passes the same path back
            (the function does NOT re-derive it from the env var
            because the request UUID is already baked in).
        project_id: GCP project ID (same as the hydrate call).
        tenant_id: Multi-tenant identifier (same as hydrate; defaults
            to ``"default"``).

    Raises:
        ValueError: tenant_id or project_id failed validation.
        FileNotFoundError: local_path doesn't exist (suggests
            hydrate was never called or the workdir was wiped
            mid-request).
        subprocess.CalledProcessError: ``gcloud storage rsync`` to
            GCS failed. Caller MAY swallow + log + retry next request
            (state will re-hydrate from the previous successful
            persist), OR surface to the user (e.g. for explicit
            "Save state now" actions).
    """
    tenant = tenant_id or _DEFAULT_TENANT_ID
    _validate_ids(tenant, project_id)

    if not os.path.isdir(local_path):
        raise FileNotFoundError(
            f"persist_workdir: local_path does not exist: {local_path!r}. "
            f"Did hydrate_workdir run successfully?"
        )

    dest = _gcs_prefix(tenant, project_id)

    _log.info(
        "storage_persist_start",
        tenant_id=tenant,
        project_id=project_id,
        local_path=local_path,
        dest=dest,
    )

    # Build the gcloud rsync command. --exclude flags filter the
    # ephemeral artifacts (_diagnostics, *.backup) defined in
    # _PERSIST_EXCLUDES.
    cmd = [
        "gcloud", "storage", "rsync",
        "--recursive",
        "--delete-unmatched-destination-objects",
    ]
    for pattern in _PERSIST_EXCLUDES:
        cmd.extend(["--exclude", pattern])
    # Trailing slash on local_path matters for the same reason as on
    # the GCS URI: tells rsync to upload the directory CONTENTS rather
    # than the directory ITSELF as a child of dest.
    src_path = local_path.rstrip("/\\") + "/"
    cmd.extend([src_path, dest])

    try:
        _run_gcloud(cmd)
    except subprocess.CalledProcessError as e:
        _log.error(
            "storage_persist_failed",
            tenant_id=tenant,
            project_id=project_id,
            local_path=local_path,
            dest=dest,
            stderr=(e.stderr or "")[:500],
        )
        raise

    _log.info(
        "storage_persist_complete",
        tenant_id=tenant,
        project_id=project_id,
        dest=dest,
    )
