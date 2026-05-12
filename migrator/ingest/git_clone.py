"""Clone Git URLs for the Migrator engine.

Supports HTTPS+PAT for GitHub, GitLab, Bitbucket, Azure DevOps.

SSH is intentionally NOT supported — Cloud Run hosting can't easily
manage SSH key material per-tenant. PAT-in-URL is the standard SaaS
clone flow.

Public API
==========
::

    result = clone_repo(
        "https://github.com/owner/repo.git",
        pat="ghp_...",       # optional; omit for public repos
        branch="main",       # optional
    )
    try:
        # use result.path with the rest of the engine
        run_migration(result.path, ...)
    finally:
        result.cleanup()

The cleanup callable wipes the temp dir. The UI layer wraps this in
a try/finally so a crashed migration still removes the checkout.

Error handling
==============
``clone_repo`` raises :class:`common.errors.PreflightError` on:
  * malformed URLs / unsupported schemes
  * authentication failures (git exit 128 with "Authentication failed")
  * timeout (default 120s; configurable)
  * non-existent repos (git exit 128 with "not found")

Each error carries a ``user_hint`` Streamlit can render inline.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from typing import Callable, Optional
from urllib.parse import urlparse, urlunparse

from common.errors import PreflightError
from common.logging import get_logger


_log = get_logger(__name__)


def _preflight(message: str, *, stage: str, reason: str, hint: str) -> PreflightError:
    """Build a PreflightError with the ``user_hint`` attribute set.

    PreflightError stores fields via **kwargs but ``user_hint`` is a
    class-level attribute the UI reads off the instance — set via
    plain assignment after construction so the UI can render a clean
    operator-facing message.
    """
    exc = PreflightError(message, stage=stage, reason=reason)
    exc.user_hint = hint
    return exc


# Per-provider PAT injection user-placeholder. URL becomes:
#   https://<user>:<token>@<host>/<path>
# Each provider expects a different "user" string before the token.
# When the host isn't in this map (self-hosted GitLab, on-prem GitHub
# Enterprise, etc.), we default to "oauth2" which is the GitLab style
# and works with most installations.
_PROVIDER_USER = {
    "github.com":    "oauth2",
    "gitlab.com":    "oauth2",
    "bitbucket.org": "x-token-auth",
    "dev.azure.com": "pat",
}

# Generously-allowed scheme list. Anything else (file://, ssh://, git://)
# is rejected. SSH would require key-management we don't have on Cloud Run.
_ALLOWED_SCHEMES = {"https", "http"}


@dataclass(frozen=True)
class CloneResult:
    """Return value from :func:`clone_repo`.

    ``path`` is the absolute path to the checked-out repo (a tmpdir).
    ``cleanup`` is a no-arg callable that deletes the tmpdir — caller
    MUST invoke it (in a finally:) so disk doesn't fill up on Cloud Run.
    """
    path: str
    cleanup: Callable[[], None]
    # Echo of the resolved branch (helpful for the UI to display
    # "Migrated from main @ abc1234" once we add SHA capture).
    branch: Optional[str] = None


def clone_repo(
    url: str,
    *,
    pat: Optional[str] = None,
    branch: Optional[str] = None,
    depth: int = 1,
    timeout_s: int = 120,
    dest_dir: Optional[str] = None,
) -> CloneResult:
    """Clone a Git URL to a temp dir and return the path.

    Args:
        url: HTTPS Git URL (``https://github.com/owner/repo.git``).
            SSH and other schemes are rejected.
        pat: Personal Access Token. When set, injected into the URL
            via the provider-specific user placeholder. When None,
            performs an unauthenticated clone (public repos only).
        branch: optional branch / tag / SHA to check out. When None,
            uses the remote's default branch (usually ``main``).
        depth: shallow clone depth. ``1`` = latest commit only (the
            default — fastest, minimal disk). Set to ``0`` for full
            history (rarely needed for IaC repos).
        timeout_s: clone timeout in seconds.
        dest_dir: explicit destination (mostly for tests). When None,
            ``tempfile.mkdtemp(prefix="migrator_clone_")`` picks one.

    Returns:
        :class:`CloneResult` with ``.path`` (the checkout) and
        ``.cleanup()`` (call when done to delete the tmpdir).

    Raises:
        :class:`PreflightError` on URL parse failure, unsupported
        scheme, missing git binary, auth failure, timeout, or repo
        not found. Each error carries a ``user_hint`` Streamlit can
        surface inline.
    """
    if not url or not url.strip():
        raise _preflight(
            "Git URL is empty",
            stage="git_clone",
            reason="empty_url",
            hint="Paste a full HTTPS Git URL like https://github.com/owner/repo.git",
        )

    parsed = urlparse(url.strip())
    if parsed.scheme not in _ALLOWED_SCHEMES:
        raise _preflight(
            f"Unsupported URL scheme '{parsed.scheme}'",
            stage="git_clone",
            reason="unsupported_scheme",
            hint=(
                "Only HTTPS Git URLs are supported. SSH URLs "
                "(git@github.com:...) can't be used on Cloud Run "
                "since we can't manage SSH keys per-tenant. Convert "
                "your URL to HTTPS form and supply a PAT below."
            ),
        )
    if not parsed.netloc:
        raise _preflight(
            f"Malformed Git URL: {url!r}",
            stage="git_clone",
            reason="malformed_url",
            hint="URL must include a host (https://<host>/owner/repo.git)",
        )

    # Build the clone URL — inject PAT if supplied.
    clone_url = _inject_pat(parsed, pat)

    # Pick a destination dir. Always use a fresh tmpdir to avoid
    # leaking content across clones of different repos.
    dest = dest_dir or tempfile.mkdtemp(prefix="migrator_clone_")

    cmd = ["git", "clone"]
    if depth and depth > 0:
        cmd += ["--depth", str(depth)]
    if branch:
        cmd += ["--branch", branch]
    cmd += ["--single-branch", clone_url, dest]

    # Don't log the clone_url verbatim — it contains the PAT. Log a
    # redacted form.
    redacted_url = _redact_url(clone_url)
    log = _log.bind(url=redacted_url, branch=branch or "(default)", depth=depth)
    log.info("git_clone_start")

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            timeout=timeout_s,
            check=False,
        )
    except subprocess.TimeoutExpired:
        _cleanup_dir(dest)
        raise _preflight(
            f"git clone timed out after {timeout_s}s",
            stage="git_clone",
            reason="clone_timeout",
            hint=(
                "Repo took too long to clone. Try a shallow clone "
                f"(--depth=1) or increase the timeout from {timeout_s}s. "
                "For very large repos consider cloning a subdirectory "
                "instead of the whole repo."
            ),
        )
    except FileNotFoundError:
        # git binary not on PATH
        _cleanup_dir(dest)
        raise _preflight(
            "git binary not found on PATH",
            stage="git_clone",
            reason="git_not_installed",
            hint=(
                "The Migrator engine needs git to clone customer repos. "
                "On Cloud Run, ensure the container image has git installed. "
                "Locally, install git and retry."
            ),
        )

    if result.returncode != 0:
        _cleanup_dir(dest)
        stderr = result.stderr.decode("utf-8", errors="replace") if result.stderr else ""
        # Redact the URL out of stderr too — git sometimes echoes it.
        stderr_redacted = _redact_text(stderr)
        log.warning(
            "git_clone_failed",
            exit_code=result.returncode,
            stderr_preview=stderr_redacted[:500],
        )
        reason, hint = _classify_clone_failure(stderr_redacted)
        raise _preflight(
            f"git clone failed (exit {result.returncode}): {stderr_redacted[:200]}",
            stage="git_clone",
            reason=reason,
            hint=hint,
        )

    log.info("git_clone_complete", dest=dest)
    return CloneResult(
        path=os.path.abspath(dest),
        cleanup=lambda: _cleanup_dir(dest),
        branch=branch,
    )


def _inject_pat(parsed_url, pat: Optional[str]) -> str:
    """Reconstruct the URL with PAT credentials (when supplied).

    Format: ``https://<provider-user>:<token>@<host>/<path>``.
    The provider-user comes from ``_PROVIDER_USER`` keyed by host.
    Self-hosted instances fall back to ``oauth2`` (GitLab-style).
    """
    if not pat:
        # No PAT — clone unauthenticated (public repos only).
        return urlunparse(parsed_url)
    host = parsed_url.netloc.split("@")[-1].lower()
    # Strip any pre-existing port suffix for the lookup
    host_no_port = host.split(":")[0]
    user = _PROVIDER_USER.get(host_no_port, "oauth2")
    new_netloc = f"{user}:{pat}@{host}"
    return urlunparse(parsed_url._replace(netloc=new_netloc))


def _redact_url(url: str) -> str:
    """Mask the password component (the PAT) for logging."""
    return re.sub(r"://([^:/@]+):([^@]+)@", r"://\1:***@", url)


def _redact_text(text: str) -> str:
    """Redact any URL-embedded PAT that git might echo into stderr."""
    return re.sub(r"://([^:/@]+):([^@\s]+)@", r"://\1:***@", text)


def _classify_clone_failure(stderr: str) -> tuple[str, str]:
    """Map git's stderr to a (reason, user_hint) pair so the UI can
    render a helpful error rather than a wall of git output."""
    low = stderr.lower()
    if "authentication failed" in low or "invalid credentials" in low:
        return ("auth_failed", (
            "Git authentication failed. Double-check the PAT has at "
            "least 'repo' (or equivalent read) scope and hasn't expired. "
            "For private repos PAT is required."
        ))
    if "could not read username" in low or "terminal prompts disabled" in low:
        return ("auth_required", (
            "Repo is private but no PAT was supplied. Provide a "
            "Personal Access Token below."
        ))
    if "repository not found" in low or "not found" in low or "404" in low:
        return ("repo_not_found", (
            "Repository not found. Check the URL (case-sensitive) "
            "and that the PAT's owner has read access."
        ))
    if "could not resolve host" in low or "name or service not known" in low:
        return ("dns_failure", (
            "Could not resolve the Git host. Check the URL and "
            "your network connectivity."
        ))
    if "branch" in low and ("not found" in low or "remote branch" in low):
        return ("branch_not_found", (
            "Branch / ref not found on the remote. Check the branch "
            "name (case-sensitive) or leave it blank to use the "
            "repository's default branch."
        ))
    return ("clone_failed", (
        "git clone failed for an unrecognised reason. Inspect the "
        "stderr snippet above; common causes: rate-limited PAT, repo "
        "moved/archived, SSO not authorised for the token."
    ))


def _cleanup_dir(path: str) -> None:
    """Remove a temp checkout dir. Best-effort — logs but doesn't raise.

    Uses an onerror handler that chmods read-only files to writable
    before retrying. Git's ``.git/objects/pack/*.pack`` files (and
    similar) are marked read-only on Windows; ``shutil.rmtree`` with
    ``ignore_errors=True`` silently leaves them undeleted, filling the
    OS tempdir over time.
    """
    if not path or not os.path.isdir(path):
        return

    def _on_rm_error(func, p, exc_info):
        # Read-only file on Windows — chmod writable and retry once.
        import stat as _stat
        try:
            os.chmod(p, _stat.S_IWRITE | _stat.S_IREAD)
            func(p)
        except OSError:
            pass

    try:
        shutil.rmtree(path, onerror=_on_rm_error)
        _log.info("git_clone_cleanup", path=path)
    except OSError as e:
        _log.warning("git_clone_cleanup_failed", path=path, error=str(e))


def detect_provider(url: str) -> Optional[str]:
    """Return the provider host slug (``github``, ``gitlab``,
    ``bitbucket``, ``azure``) or None for self-hosted / unknown.
    Helper for the UI — lets the page show a provider badge or
    PAT-scope guidance per host."""
    parsed = urlparse(url.strip() if url else "")
    host = parsed.netloc.split("@")[-1].split(":")[0].lower()
    if "github" in host:
        return "github"
    if "gitlab" in host:
        return "gitlab"
    if "bitbucket" in host:
        return "bitbucket"
    if "dev.azure.com" in host or "visualstudio.com" in host:
        return "azure"
    return None
