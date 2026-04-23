# common/terraform_path.py
"""
Single source of truth for locating the `terraform` binary.

History (why this module exists)
--------------------------------
Before this module, three different files hardcoded the Windows install
path (`importer/config.py`, `translator/config.py`, `agent_nodes.py`) and
two more shelled out to bare `terraform` and relied on PATH
(`importer/schema_oracle.py`, `detector/remediator.py`). On any machine
where Terraform wasn't at the hardcoded path AND wasn't on PATH, half
the codebase silently used one and half failed. Diagnosis was painful;
the symptom on a fresh checkout was `[WinError 2]` from one module and
`exit 127` from another, with no shared cause visible.

Resolution order (first hit wins)
---------------------------------
    1. $TERRAFORM_BINARY env var (deployment override; e.g. the path
       baked into a Cloud Run image)
    2. Platform default if the file exists:
         - Windows: C:\\Terraform\\terraform.exe
         - POSIX:   /usr/local/bin/terraform
    3. PATH lookup via shutil.which("terraform")
    4. RuntimeError with a clear install hint

Caching
-------
The resolved path is cached after the first successful lookup. This
matters in hot loops — e.g. `build_kb.py` walks every resource type and
each call ends up running `terraform`; we don't want N redundant
filesystem stats for the resolver itself.

Callers should NOT capture the result at import time if they need the
env-var override to apply later. Either call resolve_terraform_path()
at use time, or rely on `config.TERRAFORM_PATH` (which proxies through
this module lazily via PEP-562 module __getattr__).
"""

from __future__ import annotations

import os
import shutil
import sys
from typing import Optional

# Per-platform default install location. These match where the project
# has historically installed Terraform on developer machines; production
# (Cloud Run) overrides via $TERRAFORM_BINARY in the Dockerfile.
_WINDOWS_DEFAULT = r"C:\Terraform\terraform.exe"
_POSIX_DEFAULT = "/usr/local/bin/terraform"

_INSTALL_HINT = (
    "Install Terraform from https://developer.hashicorp.com/terraform/install "
    "and either:\n"
    "  (a) place the binary at the platform default path "
    "(Windows: C:\\Terraform\\terraform.exe; macOS/Linux: /usr/local/bin/terraform), or\n"
    "  (b) add it to PATH, or\n"
    "  (c) set the TERRAFORM_BINARY environment variable to its absolute path."
)

_cached: Optional[str] = None


def _platform_default() -> str:
    return _WINDOWS_DEFAULT if sys.platform.startswith("win") else _POSIX_DEFAULT


def resolve_terraform_path() -> str:
    """Return the absolute path to the `terraform` binary, or raise.

    Result is cached process-wide after the first successful resolution.
    Cache is invalidated if the cached file no longer exists (e.g. the
    binary was moved between calls), forcing re-resolution.
    """
    global _cached
    if _cached and os.path.isfile(_cached):
        return _cached

    # 1. Explicit override via env var. We require isfile() not just
    # exists() so a directory path can't accidentally satisfy the check.
    env_path = os.environ.get("TERRAFORM_BINARY")
    if env_path and os.path.isfile(env_path):
        _cached = env_path
        return _cached

    # 2. Platform default install location.
    default = _platform_default()
    if os.path.isfile(default):
        _cached = default
        return _cached

    # 3. PATH lookup. shutil.which handles .exe extension on Windows.
    on_path = shutil.which("terraform")
    if on_path:
        _cached = on_path
        return _cached

    raise RuntimeError("Could not locate the `terraform` binary.\n" + _INSTALL_HINT)


def reset_cache() -> None:
    """Clear the cached resolution. Intended for tests; not needed in
    normal use."""
    global _cached
    _cached = None
