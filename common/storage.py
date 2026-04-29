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

import fnmatch
import os
import re
import subprocess
from typing import Iterable, List, Optional

from google.cloud import storage as gcs
from google.api_core import exceptions as gcs_exceptions

from common.logging import get_logger

_log = get_logger(__name__)

# Module-singleton GCS client. Lazily constructed on first use so import-
# time cost is zero (matters for unit tests that don't touch GCS).
# Thread-safe: google-cloud-storage Client is documented as thread-safe.
_gcs_client: Optional["gcs.Client"] = None

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

# Files / directories never persisted to GCS.
#
# PUI-1Q (2026-04-30) -- semantic clarification + critical bugfix:
# An entry in this list means "IGNORE this path entirely during
# persist -- neither upload nor delete from GCS." Both halves matter:
#
#   * Upload skip (step 1 / local walk): obvious -- don't push
#     ephemeral files to GCS.
#   * Delete skip (step 4 / remote-delete loop): CRITICAL -- the
#     persist's rsync-style "delete remote blobs not in local set"
#     would otherwise wipe paths that terraform manages directly in
#     GCS (terraform-state/default.tfstate, etc.) but never writes
#     to local disk. Pre-PUI-1Q this loop did NOT consult the
#     excludes -- it deleted the GCS-backend state file every
#     persist, silently destroying every successful import. Caught
#     during the PUI-4b Detector smoke (Detector showed 0 Compliant
#     despite 8 successful imports because terraform state was gone).
#
# Entries:
#   - _diagnostics/**: translator's YAML blueprint dump
#     (MTAGENT_PERSIST_BLUEPRINTS=0 disables it anyway in SaaS mode,
#     but excluding here is belt-and-braces)
#   - *.backup: terraform's auto-backup files (redundant given GCS
#     object versioning); also *.tfstate.backup variants
#   - terraform.tfstate (root only): a stray local state at the workdir
#     root MUST NOT be rsynced back -- with PSA-5's GCS backend the
#     canonical state lives in terraform-state/default.tfstate and a
#     local terraform.tfstate would only exist if init failed to wire
#     the backend (in which case uploading it would clobber GCS state).
#   - terraform.tfstate.lock.info: terraform's local lock file (if
#     any); always ephemeral
#   - terraform-state/**: PUI-1Q FIX. The GCS-backend state directory.
#     terraform writes here directly via the gcs backend; local disk
#     never has these files; persist's delete-orphans loop was wiping
#     them every request. Marking the whole subtree as ignored fixes
#     the data-loss bug.
#   - .terraform/terraform.tfstate: PUI-1Q FIX. terraform's BACKEND
#     CACHE (records which backend is active + workspace metadata).
#     Pre-PUI-1Q the basename "terraform.tfstate" pattern over-matched
#     this file and excluded it from upload, so on next hydrate the
#     backend cache was missing and terraform commands had to re-init
#     blindly. Explicit path entry makes the intent clear and the new
#     bugfix in step 4 stops it from being deleted remotely too.
_PERSIST_EXCLUDES = (
    "_diagnostics/**",
    "*.backup",
    "*.tfstate.backup",
    "*.tfstate.*.backup",
    "terraform.tfstate",
    "terraform.tfstate.lock.info",
    "terraform-state/**",
    ".terraform/terraform.tfstate",
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


def _get_gcs_client() -> "gcs.Client":
    """Return the module-singleton google-cloud-storage Client.

    Lazy-initialized; uses Application Default Credentials (ADC)
    which Cloud Run's metadata server provides automatically. No
    explicit auth dance needed -- the SDK detects the runtime SA
    via the metadata server without requiring `gcloud auth login`
    or `gcloud config set account` (the gcloud CLI's notorious
    pain point in containerized environments).

    Pulled into a helper so tests can patch a single seam:
    ``patch("common.storage._get_gcs_client", return_value=mock_client)``.
    """
    global _gcs_client
    if _gcs_client is None:
        _gcs_client = gcs.Client()
    return _gcs_client


def _matches_any_pattern(relpath: str, patterns: Iterable[str]) -> bool:
    """Return True if ``relpath`` matches any of the glob ``patterns``.

    Used by ``persist_workdir`` to filter out ephemeral files (see
    _PERSIST_EXCLUDES). Supports both regular fnmatch globs (``*.backup``)
    AND the ``dir/**`` "anything under this directory" shorthand
    (``_diagnostics/**``) since fnmatch doesn't natively understand ``**``.

    All paths use forward slashes (matched against the canonical
    POSIX-style relpath) regardless of OS, mirroring how gcloud
    storage rsync's --exclude flag interpreted them.
    """
    rp = relpath.replace("\\", "/")
    for pattern in patterns:
        # `dir/**` shorthand: match anything under `dir/`.
        if pattern.endswith("/**"):
            prefix = pattern[:-3] + "/"
            if rp.startswith(prefix) or rp == pattern[:-3]:
                return True
        # Standard fnmatch for everything else.
        if fnmatch.fnmatch(rp, pattern):
            return True
        # Also match basename so `*.backup` catches nested files.
        if fnmatch.fnmatch(os.path.basename(rp), pattern):
            return True
    return False


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

    bucket_name = state_bucket()
    prefix = f"tenants/{tenant}/projects/{project_id}/"
    src = f"gs://{bucket_name}/{prefix}"

    _log.info(
        "storage_hydrate_start",
        tenant_id=tenant,
        project_id=project_id,
        src=src,
        local_path=local_path,
    )

    # SDK-based hydrate (PUI-1 SMOKE layer 5 fix):
    #   * List blobs under prefix; each blob.name is "tenants/<t>/projects/<p>/relpath..."
    #   * Strip prefix to get the relative path; join with local_path
    #   * Make parent dirs as needed; download blob to that local file
    #
    # Why SDK over `gcloud storage rsync` subprocess:
    #   * Uses ADC via metadata server (auto-detected in Cloud Run).
    #     gcloud CLI requires explicit `gcloud auth login` or
    #     `gcloud config set account` PLUS a working credential store
    #     -- a brittle chain that broke in our Cloud Run container.
    #   * No subprocess fork overhead.
    #   * Proper exception types (NotFound, Forbidden, etc.) instead
    #     of stderr-string parsing.
    client = _get_gcs_client()
    try:
        bucket = client.bucket(bucket_name)
        blob_iter = client.list_blobs(bucket, prefix=prefix)
        downloaded = 0
        for blob in blob_iter:
            # Skip the prefix itself if it shows up as a 0-byte placeholder.
            if blob.name == prefix or blob.name.endswith("/"):
                continue
            relpath = blob.name[len(prefix):]
            if not relpath:
                continue
            dest_file = os.path.join(local_path, relpath.replace("/", os.sep))
            os.makedirs(os.path.dirname(dest_file), exist_ok=True)
            blob.download_to_filename(dest_file)
            downloaded += 1
    except gcs_exceptions.NotFound:
        # First-run-for-this-project case: the bucket exists but the
        # prefix has never been written to. Listing returns empty (no
        # exception in modern SDK), but defensive catch in case some
        # SDK versions raise NotFound on a missing prefix. Either way
        # ends here as "0 blobs to download" which is correct behavior.
        _log.info(
            "storage_hydrate_skipped_source_missing",
            tenant_id=tenant,
            project_id=project_id,
            src=src,
            local_path=local_path,
            reason="GCS source prefix doesn't exist yet; "
                   "starting with empty workdir (first run for "
                   "this project)",
        )
        return local_path
    except Exception as e:
        _log.error(
            "storage_hydrate_failed",
            tenant_id=tenant,
            project_id=project_id,
            src=src,
            local_path=local_path,
            error_type=type(e).__name__,
            error=str(e)[:500],
        )
        raise

    if downloaded == 0:
        # Modern SDK returns an empty iterator (no NotFound) when
        # nothing matches the prefix -- the COMMON first-run path.
        _log.info(
            "storage_hydrate_skipped_source_missing",
            tenant_id=tenant,
            project_id=project_id,
            src=src,
            local_path=local_path,
            reason="GCS source prefix has no objects; "
                   "starting with empty workdir (first run for "
                   "this project)",
        )
    else:
        _log.info(
            "storage_hydrate_complete",
            tenant_id=tenant,
            project_id=project_id,
            local_path=local_path,
            objects_downloaded=downloaded,
        )

    # PUI-1R (2026-04-30): restore POSIX +x bit on terraform provider
    # binaries.
    #
    # Why: GCS does NOT preserve POSIX file permissions in object
    # metadata. terraform downloads provider binaries (e.g.
    # `.terraform/providers/registry.terraform.io/hashicorp/google/
    # 7.29.0/linux_amd64/terraform-provider-google_v7.29.0_x5`) with
    # the executable bit set; persist uploads them to GCS losing the
    # bit; hydrate downloads them back with default 0644 (rw-r--r--).
    # Subsequent `terraform plan/apply/init` calls fail with::
    #
    #   Failed to obtain provider schema: ... fork/exec
    #   .terraform/providers/.../terraform-provider-google_v7.29.0_x5:
    #   permission denied
    #
    # Fix: walk `.terraform/providers/**` after hydrate, chmod 0o755
    # any file matching `terraform-provider-*` (the standard naming
    # convention for HashiCorp + community providers). 0o755 = owner
    # rwx + group/other rx, mirroring what `terraform init` originally
    # set when downloading from the registry.
    #
    # Caught by PUI-4j SaaS Detector smoke (Restore button) where the
    # first cloud-mutating action after a hydrate cycle hit this for
    # the first time. Read-only paths (state_pull, the inventory
    # gcloud calls) didn't expose the bug because they don't fork the
    # provider binary.
    providers_root = os.path.join(local_path, ".terraform", "providers")
    if os.path.isdir(providers_root):
        chmodded = 0
        for root, _dirs, files in os.walk(providers_root):
            for fname in files:
                if not fname.startswith("terraform-provider-"):
                    continue
                fpath = os.path.join(root, fname)
                try:
                    os.chmod(fpath, 0o755)
                    chmodded += 1
                except OSError as chmod_err:
                    _log.warning(
                        "provider_binary_chmod_failed",
                        tenant_id=tenant,
                        project_id=project_id,
                        path=fpath,
                        error=str(chmod_err),
                    )
        if chmodded:
            _log.info(
                "provider_binaries_chmodded",
                tenant_id=tenant,
                project_id=project_id,
                count=chmodded,
                providers_root=providers_root,
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

    bucket_name = state_bucket()
    prefix = f"tenants/{tenant}/projects/{project_id}/"
    dest = f"gs://{bucket_name}/{prefix}"

    _log.info(
        "storage_persist_start",
        tenant_id=tenant,
        project_id=project_id,
        local_path=local_path,
        dest=dest,
    )

    # SDK-based persist (PUI-1 SMOKE layer 5 fix):
    #   1. Walk local tree, build {relpath -> absolute_local_path}
    #      for every file NOT matching _PERSIST_EXCLUDES.
    #   2. List existing blobs under prefix to find ones to delete
    #      (the rsync --delete-unmatched-destination-objects semantic).
    #   3. Upload each local file (skip-if-unchanged would require
    #      MD5 comparison; for Round-1 simplicity we always upload --
    #      GCS object versioning means cost is dominated by storage,
    #      not request count).
    #   4. Delete blobs that exist remotely but not locally.
    #
    # Why SDK over `gcloud storage rsync` subprocess: see hydrate_workdir
    # for the auth + error-handling rationale.
    client = _get_gcs_client()
    bucket = client.bucket(bucket_name)

    try:
        # Step 1: walk local, filter excludes
        local_files: dict = {}  # relpath (POSIX) -> abs local path
        for root, _dirs, files in os.walk(local_path):
            for fname in files:
                abs_path = os.path.join(root, fname)
                rel = os.path.relpath(abs_path, local_path).replace(
                    os.sep, "/",
                )
                if _matches_any_pattern(rel, _PERSIST_EXCLUDES):
                    continue
                local_files[rel] = abs_path

        # Step 2: collect existing remote blob names (relative to prefix)
        # so we can compute the set difference for deletion.
        remote_blobs: dict = {}  # relpath (POSIX) -> Blob
        for blob in client.list_blobs(bucket, prefix=prefix):
            if blob.name == prefix or blob.name.endswith("/"):
                continue
            rel = blob.name[len(prefix):]
            remote_blobs[rel] = blob

        # Step 3: upload all local files (overwrites if changed; GCS
        # object versioning preserves the prior generation).
        uploaded = 0
        for rel, abs_path in local_files.items():
            blob = bucket.blob(prefix + rel)
            blob.upload_from_filename(abs_path)
            uploaded += 1

        # Step 4: delete remote blobs that no longer exist locally,
        # EXCEPT those matching _PERSIST_EXCLUDES (PUI-1Q fix).
        #
        # Pre-PUI-1Q this loop did NOT consult the excludes -- it
        # deleted ANY remote blob not present locally. That semantic
        # is wrong for paths that terraform writes directly to GCS
        # via its GCS backend (terraform-state/default.tfstate +
        # the .tflock siblings). Those files NEVER exist on local
        # disk, so step 1's local walk never enters them in
        # local_files, so step 4 was wiping them every persist --
        # silently destroying every successful import (caught during
        # PUI-4b Detector smoke; Detector showed 0 Compliant despite
        # 8 successful imports because terraform state was gone).
        #
        # New semantic: an _PERSIST_EXCLUDES entry means "ignore this
        # path entirely -- neither upload nor delete remotely." The
        # remote stays untouched on persist; if the operator wants to
        # wipe it they use reset_workdir (which uses a different,
        # explicit code path).
        deleted = 0
        for rel, blob in remote_blobs.items():
            if rel in local_files:
                continue
            if _matches_any_pattern(rel, _PERSIST_EXCLUDES):
                # Belongs to a remote-managed path (terraform-state/,
                # backend cache, etc.). Don't delete -- terraform owns it.
                continue
            blob.delete()
            deleted += 1
    except Exception as e:
        _log.error(
            "storage_persist_failed",
            tenant_id=tenant,
            project_id=project_id,
            local_path=local_path,
            dest=dest,
            error_type=type(e).__name__,
            error=str(e)[:500],
        )
        raise

    _log.info(
        "storage_persist_complete",
        tenant_id=tenant,
        project_id=project_id,
        dest=dest,
        objects_uploaded=uploaded,
        objects_deleted=deleted,
    )


# ----------------------------------------------------------------------
# PUI-1B v2: read-back helpers for the UI's "Generated files" view.
#
# After a successful import the operator wants to SEE the generated
# .tf files (review, copy, download). The UI doesn't have access to
# the per-request /tmp workdir (Cloud Run's filesystem is per-container,
# ephemeral), so we read from the persisted GCS state.
# ----------------------------------------------------------------------

def list_workdir_tf_files(
    project_id: str,
    *,
    tenant_id: Optional[str] = None,
) -> List[dict]:
    """List the .tf files persisted for a (tenant, project), with
    per-file status (imported vs quarantined).

    Returns a list of dicts ordered first by status (imported first),
    then alphabetically by name::

        [
          {"name": "google_storage_bucket_x.tf",
           "size_bytes": 1234,
           "status": "imported",
           "error_preview": None},
          {"name": "google_container_cluster_x.tf",
           "size_bytes": 5678,
           "status": "needs_attention",
           "error_preview": "Error: Conflicting configuration..."},
        ]

    Filters to user-relevant .tf files only -- excludes infrastructure
    files (.terraform.lock.hcl, _backend_seed.tf, _providers_seed.tf)
    that the operator didn't author and shouldn't see in the
    "Generated files" view.

    Status semantics (PUI-1B v3.3):
      * `imported`: file lives at top-level workdir (terraform import
        + plan-verify both succeeded; HCL is canonical)
      * `needs_attention`: file lives under `_quarantine/` (terraform
        import succeeded but plan-verify failed; HCL has issues the
        operator should review). The matching `<name>.quarantine.txt`
        sidecar (also in `_quarantine/`) holds the verbatim terraform
        error -- we read its first ~300 chars as `error_preview`.

    Args:
        project_id: GCP project (path-traversal-validated).
        tenant_id: Multi-tenant identifier (defaults "default").

    Returns:
        List of file metadata dicts. Empty list if no .tf files
        exist (e.g., before the first successful import).

    Raises:
        ValueError: project_id / tenant_id failed validation.
        Other google.api_core exceptions for genuine API failures.
    """
    tenant = tenant_id or _DEFAULT_TENANT_ID
    _validate_ids(tenant, project_id)

    bucket_name = state_bucket()
    prefix = f"tenants/{tenant}/projects/{project_id}/"

    _INFRA_FILES = frozenset((
        ".terraform.lock.hcl",
        "_backend_seed.tf",
        "_providers_seed.tf",
    ))

    client = _get_gcs_client()
    bucket = client.bucket(bucket_name)

    # Two passes:
    #   Pass 1: collect all relevant blobs, group by status. Also
    #           collect quarantine.txt sidecars to fetch error previews.
    imported: List[dict] = []
    quarantined: List[dict] = []
    quarantine_sidecars: dict = {}  # tf_name -> sidecar blob path

    for blob in client.list_blobs(bucket, prefix=prefix):
        if blob.name == prefix or blob.name.endswith("/"):
            continue
        rel = blob.name[len(prefix):]
        # Quarantined .tf -> needs_attention bucket
        if rel.startswith("_quarantine/"):
            inner = rel[len("_quarantine/"):]
            if inner.endswith(".quarantine.txt"):
                # Sidecar -- record path for later read
                tf_name = inner[:-len(".quarantine.txt")]
                quarantine_sidecars[tf_name] = blob.name
                continue
            if inner.endswith(".tf"):
                quarantined.append({
                    "name": inner,
                    "size_bytes": blob.size or 0,
                    "status": "needs_attention",
                    "error_preview": None,  # filled in pass 2
                    "_blob_path": blob.name,
                })
            continue
        # Other nested paths (.terraform/, snapshots/) -> skip
        if "/" in rel:
            continue
        if not rel.endswith(".tf"):
            continue
        if rel in _INFRA_FILES:
            continue
        imported.append({
            "name": rel,
            "size_bytes": blob.size or 0,
            "status": "imported",
            "error_preview": None,
        })

    # Pass 2: read quarantine.txt sidecars for error previews.
    # Each is a small text file (~few KB); cheap to download inline.
    #
    # PUI-1F v3.1 (2026-04-29 smoke 4 fix): two improvements over the
    # original 300-char preview:
    #
    # (a) Skip the preamble. The sidecar template (from
    #     importer/quarantine.py) starts with ~250 chars of metadata
    #     ("Quarantined: <addr>\nSource file: <name>\nReason:\n
    #     Auto-quarantined after...\n\nTerraform error (truncated):\n")
    #     before the actual terraform output begins. The previous
    #     300-char preview consumed the entire preamble and showed
    #     ZERO of the actual diagnostic. Now we skip past
    #     "Terraform error (truncated):\n" if present.
    #
    # (b) Anchor on the actual "Error:" line when possible. terraform
    #     plan output starts with progress lines ("Refreshing
    #     state... [id=...]") that aren't useful for diagnosis. The
    #     "Error:" block (or "│ Error:" with the gutter) is what the
    #     operator needs to see. If present in the text, we slice
    #     from there.
    #
    # (c) Bumped cap 300 -> 1500 chars. Most provider error blocks +
    #     2-3 lines of HCL context fit in 1500. Beyond that, the
    #     operator should open the full sidecar (PUI-1F v3.1 added
    #     a "Show full quarantine details" button on each card).
    for q in quarantined:
        sidecar_path = quarantine_sidecars.get(q["name"])
        if not sidecar_path:
            continue
        try:
            sidecar_blob = client.bucket(bucket_name).blob(sidecar_path)
            text = sidecar_blob.download_as_text()

            # (a) Skip the preamble.
            preview_start = 0
            marker = "Terraform error (truncated):\n"
            idx_marker = text.find(marker)
            if idx_marker >= 0:
                preview_start = idx_marker + len(marker)

            # (b) Anchor on the actual Error: block if present.
            # Look for both styles: bare "Error:" and the gutter form
            # "│ Error:" terraform uses on multi-line errors.
            for needle in ("\n│ Error:", "\nError:"):
                err_idx = text.find(needle, preview_start)
                if err_idx >= 0:
                    preview_start = err_idx + 1  # past the leading \n
                    break

            # (c) 1500-char preview from the chosen anchor.
            q["error_preview"] = text[
                preview_start:preview_start + 1500
            ].strip()
        except Exception:  # noqa: BLE001
            # Sidecar read failure is non-fatal -- file still listed
            # without error preview.
            pass

    # Sort: imported first (alphabetical), then needs_attention
    # (alphabetical). This puts the celebratory rows on top and
    # the attention-needed rows below where the operator focuses.
    imported.sort(key=lambda f: f["name"])
    quarantined.sort(key=lambda f: f["name"])
    return imported + quarantined


def reset_workdir(
    project_id: str,
    *,
    tenant_id: Optional[str] = None,
) -> dict:
    """Wipe ALL persisted state for a (tenant, project) pair from GCS.

    PUI-1C (2026-04-29): customer-grade replacement for the operator-
    grade ``gcloud storage rm -r gs://.../tenants/<t>/projects/<p>/``
    workflow. Performs a single batch delete across the entire prefix
    (Terraform .tf files, _quarantine sidecars, .terraform/ provider
    caches, terraform-state/, snapshots/, _diagnostics/ -- everything).

    The deletion includes ALL versioned generations (not just the
    live ones), so the project comes back to truly first-run state.
    Without versioning-aware delete, re-importing the same resources
    would inherit the prior generation's metadata (object locks,
    custom-time, etc.) and behave unpredictably.

    Args:
        project_id: GCP project ID being reset.
        tenant_id: Multi-tenant identifier (defaults "default").

    Returns:
        Dict with operational counters for audit-log emission and UI
        display::

            {
              "deleted_blobs": 47,         # live generation deletes
              "deleted_versions": 152,     # archived generation deletes
              "prefix": "tenants/default/projects/dev-proj-470211/",
              "bucket": "mtagent-state-dev",
            }

        ``deleted_blobs == 0 and deleted_versions == 0`` is a valid
        no-op result (prefix was already empty); callers should
        render this as "Nothing to reset" rather than as failure.

    Raises:
        ValueError: project_id / tenant_id failed validation.
        google.api_core exceptions for genuine API failures (auth,
            permissions, network). Caller should surface to UI.
    """
    tenant = tenant_id or _DEFAULT_TENANT_ID
    _validate_ids(tenant, project_id)

    bucket_name = state_bucket()
    prefix = f"tenants/{tenant}/projects/{project_id}/"

    _log.info(
        "storage_reset_start",
        tenant_id=tenant,
        project_id=project_id,
        bucket=bucket_name,
        prefix=prefix,
    )

    client = _get_gcs_client()
    bucket = client.bucket(bucket_name)

    deleted_blobs = 0
    deleted_versions = 0

    # First pass: delete LIVE generations. With versioning ON in the
    # bucket, this creates a "deletion marker" but the prior generation
    # remains accessible. Counted separately so the caller can show
    # "X live + Y archived" if that detail matters in the UI.
    for blob in client.list_blobs(bucket, prefix=prefix):
        try:
            blob.delete()
            deleted_blobs += 1
        except Exception as e:  # noqa: BLE001 -- best-effort batch delete
            _log.warning(
                "storage_reset_blob_delete_failed",
                tenant_id=tenant,
                project_id=project_id,
                blob_name=blob.name,
                error_type=type(e).__name__,
                error=str(e)[:200],
            )

    # Second pass: delete ARCHIVED generations (versioned objects).
    # With ``versions=True``, list_blobs returns one entry per
    # generation. We've already deleted the live ones in pass 1; here
    # we sweep the archive so the project is genuinely empty.
    #
    # API: blob.delete(generation=blob.generation) targets a specific
    # generation; without it, delete() targets the live one.
    for blob in client.list_blobs(bucket, prefix=prefix, versions=True):
        try:
            blob.delete()
            deleted_versions += 1
        except Exception as e:  # noqa: BLE001 -- best-effort
            _log.warning(
                "storage_reset_version_delete_failed",
                tenant_id=tenant,
                project_id=project_id,
                blob_name=blob.name,
                generation=blob.generation,
                error_type=type(e).__name__,
                error=str(e)[:200],
            )

    result = {
        "deleted_blobs": deleted_blobs,
        "deleted_versions": deleted_versions,
        "prefix": prefix,
        "bucket": bucket_name,
    }
    _log.info(
        "storage_reset_complete",
        tenant_id=tenant,
        project_id=project_id,
        **result,
    )
    return result


def read_quarantine_sidecar(
    project_id: str,
    tf_filename: str,
    *,
    tenant_id: Optional[str] = None,
) -> str:
    """Read the full quarantine sidecar (`<name>.tf.quarantine.txt`).

    PUI-1F v3.1 (2026-04-29 smoke 4 follow-up): operator-facing
    "Show full quarantine details" UI needs the verbatim sidecar
    text -- not the truncated preview from list_workdir_tf_files.
    Most quarantine errors are 500-3000 chars; cheap to download
    inline when the operator opens an expander.

    Args:
        project_id: GCP project (validated).
        tf_filename: BARE basename of the quarantined .tf file
            (e.g. "google_cloud_run_v2_service_poc_cloudrun.tf").
            Path-traversal-validated. Caller passes the same name
            shown in the UI's "Needs attention" list.
        tenant_id: Multi-tenant identifier (defaults "default").

    Returns:
        Full sidecar content as a UTF-8 string.

    Raises:
        ValueError: project_id / tenant_id failed validation, OR
            tf_filename contains a path separator.
        google.api_core.exceptions.NotFound: sidecar doesn't exist
            (older quarantine where reason file write failed; UI
            should catch + show "no sidecar available").
    """
    tenant = tenant_id or _DEFAULT_TENANT_ID
    _validate_ids(tenant, project_id)
    if (
        "/" in tf_filename
        or "\\" in tf_filename
        or tf_filename.startswith(".")
    ):
        raise ValueError(
            f"tf_filename must be a bare basename, got {tf_filename!r}",
        )

    bucket_name = state_bucket()
    # Sidecar naming convention: dst + ".quarantine.txt" where dst is
    # the moved .tf path. So sidecar = <name>.tf.quarantine.txt
    # (with .tf infix). See importer/quarantine.py:166.
    blob_path = (
        f"tenants/{tenant}/projects/{project_id}/_quarantine/"
        f"{tf_filename}.quarantine.txt"
    )
    client = _get_gcs_client()
    blob = client.bucket(bucket_name).blob(blob_path)
    return blob.download_as_text()


def read_workdir_file(
    project_id: str,
    filename: str,
    *,
    tenant_id: Optional[str] = None,
    from_quarantine: bool = False,
) -> str:
    """Download a single file's content from the persisted workdir.

    Used by the UI's "Generated files" expander to show .tf content
    inline (st.code with HCL syntax highlighting). PUI-1B v3.3:
    `from_quarantine=True` reads from `_quarantine/<filename>` instead
    of the top-level workdir, supporting the needs-attention view.

    Args:
        project_id: GCP project.
        filename: BARE filename (e.g. "google_storage_bucket_x.tf").
            Must NOT contain path separators -- defensive against
            client-side tampering.
        tenant_id: Multi-tenant identifier (defaults "default").
        from_quarantine: When True, read from `_quarantine/<filename>`
            (the needs-attention bucket). When False (default), read
            the top-level imported file.

    Returns:
        File content as a string (UTF-8 decoded).

    Raises:
        ValueError: project_id / tenant_id failed validation, OR
            filename contains a path separator.
        google.api_core.exceptions.NotFound: file doesn't exist.
    """
    tenant = tenant_id or _DEFAULT_TENANT_ID
    _validate_ids(tenant, project_id)

    if "/" in filename or "\\" in filename or filename.startswith("."):
        raise ValueError(
            f"filename must be a bare basename, got {filename!r}",
        )

    bucket_name = state_bucket()
    if from_quarantine:
        blob_path = (
            f"tenants/{tenant}/projects/{project_id}/_quarantine/{filename}"
        )
    else:
        blob_path = f"tenants/{tenant}/projects/{project_id}/{filename}"
    client = _get_gcs_client()
    blob = client.bucket(bucket_name).blob(blob_path)
    return blob.download_as_text()


# ----------------------------------------------------------------------
# PUI-3 Translator surface helpers (2026-04-29).
# Translator outputs land at:
#   gs://<bucket>/tenants/<t>/projects/<p>/translated/<target>/*.tf
# where <target> is "aws" or "azure". Each helper here is per-target so
# the UI can list/read/reset one target's outputs without touching the
# others (e.g. operator may have translated to AWS but not Azure -- the
# Azure listing should return empty cleanly, not error).
# ----------------------------------------------------------------------


def _validate_target_cloud(target_cloud: str) -> str:
    """Lowercase + validate. Raises ValueError on anything else."""
    target = (target_cloud or "").strip().lower()
    if target not in ("aws", "azure"):
        raise ValueError(
            f"target_cloud must be 'aws' or 'azure'; got "
            f"{target_cloud!r}"
        )
    return target


def list_translated_files(
    project_id: str,
    target_cloud: str,
    *,
    tenant_id: Optional[str] = None,
) -> List[dict]:
    """List the translator-output ``.tf`` files for a (project, target).

    Returns a list of dicts ordered alphabetically by filename::

        [
          {"name": "aws_storage_bucket_poc.tf",
           "size_bytes": 1234},
          ...
        ]

    Mirrors ``list_workdir_tf_files`` for the importer; intentionally
    simpler because the translator doesn't have a quarantine concept
    yet (validation failures land in the per-file FileOutcome instead
    of moving the .tf to a sidecar location).

    Args:
        project_id: GCP project (validated).
        target_cloud: "aws" or "azure" (validated).
        tenant_id: Multi-tenant identifier (defaults "default").

    Returns:
        List of file metadata dicts. Empty list if the operator hasn't
        run a Translate for this (project, target) yet.

    Raises:
        ValueError: project_id / tenant_id / target_cloud failed
            validation.
    """
    tenant = tenant_id or _DEFAULT_TENANT_ID
    _validate_ids(tenant, project_id)
    target = _validate_target_cloud(target_cloud)

    bucket_name = state_bucket()
    prefix = (
        f"tenants/{tenant}/projects/{project_id}/translated/{target}/"
    )

    client = _get_gcs_client()
    bucket = client.bucket(bucket_name)

    out: List[dict] = []
    for blob in client.list_blobs(bucket, prefix=prefix):
        if blob.name == prefix or blob.name.endswith("/"):
            continue
        rel = blob.name[len(prefix):]
        # Skip nested subdirectories (defensive; translator doesn't
        # nest today but a future iteration might).
        if "/" in rel:
            continue
        if not rel.endswith(".tf"):
            continue
        out.append({
            "name": rel,
            "size_bytes": blob.size or 0,
        })

    out.sort(key=lambda f: f["name"])
    return out


def read_translated_file(
    project_id: str,
    target_cloud: str,
    filename: str,
    *,
    tenant_id: Optional[str] = None,
) -> str:
    """Download a single translated file's content as a UTF-8 string.

    Used by the Translator page's "Generated translated files" expander
    to show .tf content inline (st.code with HCL syntax highlighting).

    Args:
        project_id: GCP project.
        target_cloud: "aws" or "azure".
        filename: BARE basename (no path separators). Path-traversal
            validated.
        tenant_id: Multi-tenant identifier (defaults "default").

    Returns:
        File content as a UTF-8 string.

    Raises:
        ValueError: any input failed validation.
        google.api_core.exceptions.NotFound: file doesn't exist.
    """
    tenant = tenant_id or _DEFAULT_TENANT_ID
    _validate_ids(tenant, project_id)
    target = _validate_target_cloud(target_cloud)

    if (
        "/" in filename
        or "\\" in filename
        or filename.startswith(".")
    ):
        raise ValueError(
            f"filename must be a bare basename, got {filename!r}",
        )

    bucket_name = state_bucket()
    blob_path = (
        f"tenants/{tenant}/projects/{project_id}/translated/{target}/"
        f"{filename}"
    )
    client = _get_gcs_client()
    blob = client.bucket(bucket_name).blob(blob_path)
    return blob.download_as_text()


def reset_translated(
    project_id: str,
    target_cloud: str,
    *,
    tenant_id: Optional[str] = None,
) -> dict:
    """Wipe ALL translated-output files for a (project, target).

    Targeted version of ``reset_workdir`` -- only deletes the
    ``translated/<target>/`` subtree, leaves the operator's imported
    .tf files at the project root untouched. Use when the operator
    wants to redo a translation cleanly without losing their import
    state.

    Args:
        project_id: GCP project (validated).
        target_cloud: "aws" or "azure" (validated).
        tenant_id: Multi-tenant identifier (defaults "default").

    Returns:
        Dict with operational counters::

            {
              "deleted_blobs": 7,
              "deleted_versions": 12,
              "prefix": "tenants/default/projects/.../translated/aws/",
              "bucket": "mtagent-state-dev",
              "target_cloud": "aws",
            }
    """
    tenant = tenant_id or _DEFAULT_TENANT_ID
    _validate_ids(tenant, project_id)
    target = _validate_target_cloud(target_cloud)

    bucket_name = state_bucket()
    prefix = (
        f"tenants/{tenant}/projects/{project_id}/translated/{target}/"
    )

    _log.info(
        "storage_reset_translated_start",
        tenant_id=tenant,
        project_id=project_id,
        target_cloud=target,
        bucket=bucket_name,
        prefix=prefix,
    )

    client = _get_gcs_client()
    bucket = client.bucket(bucket_name)

    deleted_blobs = 0
    deleted_versions = 0

    for blob in client.list_blobs(bucket, prefix=prefix):
        try:
            blob.delete()
            deleted_blobs += 1
        except Exception as e:  # noqa: BLE001
            _log.warning(
                "storage_reset_translated_blob_delete_failed",
                tenant_id=tenant,
                project_id=project_id,
                target_cloud=target,
                blob_name=blob.name,
                error=str(e)[:200],
            )

    for blob in client.list_blobs(bucket, prefix=prefix, versions=True):
        try:
            blob.delete()
            deleted_versions += 1
        except Exception as e:  # noqa: BLE001
            _log.warning(
                "storage_reset_translated_version_delete_failed",
                tenant_id=tenant,
                project_id=project_id,
                target_cloud=target,
                blob_name=blob.name,
                generation=blob.generation,
                error=str(e)[:200],
            )

    result = {
        "deleted_blobs": deleted_blobs,
        "deleted_versions": deleted_versions,
        "prefix": prefix,
        "bucket": bucket_name,
        "target_cloud": target,
    }
    _log.info(
        "storage_reset_translated_complete",
        tenant_id=tenant,
        project_id=project_id,
        **result,
    )
    return result
