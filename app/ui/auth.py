"""Tenant identity resolution for the Migrator UI on Cloud Run.

When the platform is deployed on Cloud Run + IAP, every authenticated
request carries the operator's identity in HTTP headers Google
injects. The Migrator engine needs that identity for two reasons:

1. **Multi-tenant snapshot isolation** — the result_persistence
   registry is keyed by ``user_key`` (typically ``<tenant>::<project>``).
   Without an identity, all tenants share the same registry slot →
   one operator's refresh-restore shows another's last run.
2. **Per-tenant audit logging** — structured-log lines tagged with
   ``tenant_id`` let us trace failures back to a specific customer.

Resolution order (first non-empty wins):
  1. ``X-Goog-Authenticated-User-Email`` HTTP header (Cloud Run + IAP)
  2. ``X-Goog-Authenticated-User-Id`` HTTP header (IAP, when email
     is hidden)
  3. ``MIGRATOR_TENANT_ID`` env var (Cloud Run override per service /
     local dev)
  4. ``"default"`` (single-tenant local dev)

The function is best-effort: any failure to read headers degrades to
the env-var path. The page sidebar surfaces whichever identity
resolved so operators see "you are signed in as X" inline.
"""

from __future__ import annotations

import os
from typing import Optional


# IAP header names. Google strips and re-injects these so client-side
# spoofing is blocked. Email is preferred (human-readable); the ID
# header is the fallback when email is masked by IAP config.
_IAP_EMAIL_HEADER = "X-Goog-Authenticated-User-Email"
_IAP_ID_HEADER    = "X-Goog-Authenticated-User-Id"


def resolve_tenant_id() -> str:
    """Return the operator's tenant slug or ``"default"``.

    Tries IAP headers first, then env var. Safe to call from any
    Streamlit page — never raises.
    """
    # 1. IAP headers via Streamlit's request context. The .context.headers
    #    attribute landed in Streamlit 1.37 — earlier versions raise
    #    AttributeError, which we treat as "not on Streamlit / no headers".
    header_val = _read_streamlit_header(_IAP_EMAIL_HEADER) \
                 or _read_streamlit_header(_IAP_ID_HEADER)
    if header_val:
        # IAP prefixes the value with "accounts.google.com:" — strip
        # so the tenant_id is just the email / user-id.
        if ":" in header_val:
            header_val = header_val.split(":", 1)[1]
        return _sanitize(header_val)

    # 2. Env-var override (Cloud Run service-level + local dev).
    env_val = os.environ.get("MIGRATOR_TENANT_ID", "").strip()
    if env_val:
        return _sanitize(env_val)

    # 3. Single-tenant default.
    return "default"


def _read_streamlit_header(name: str) -> Optional[str]:
    """Safely fetch a request header from Streamlit. Returns None when
    headers aren't available (e.g., running tests, older Streamlit)."""
    try:
        import streamlit as st  # local import — keeps module importable
                                # in non-Streamlit contexts (tests, CLI).
        ctx = getattr(st, "context", None)
        if ctx is None:
            return None
        headers = getattr(ctx, "headers", None)
        if not headers:
            return None
        # Streamlit's headers dict is case-insensitive; .get works.
        return headers.get(name)
    except Exception:  # noqa: BLE001 — best-effort
        return None


def _sanitize(s: str) -> str:
    """Normalize an identifier to a registry-safe slug.

    Email / user-id values can contain ``@``, ``.``, ``+``, etc. The
    registry uses tenant_id as part of file paths + JSON keys, so we
    keep only [A-Za-z0-9_.@-] which works in both contexts (Cloud
    Storage object names + Linux filesystem paths)."""
    import re as _re
    cleaned = _re.sub(r"[^A-Za-z0-9_.@-]+", "_", s.strip()).strip("_")
    return cleaned or "default"


def auth_status_banner() -> str:
    """One-line human-readable summary of the current identity.
    Pages call this for the sidebar / debug banner."""
    tid = resolve_tenant_id()
    if tid == "default":
        return "Single-tenant local mode (no IAP / no MIGRATOR_TENANT_ID)"
    source = (
        "Cloud Run IAP"
        if _read_streamlit_header(_IAP_EMAIL_HEADER) or _read_streamlit_header(_IAP_ID_HEADER)
        else "MIGRATOR_TENANT_ID env"
    )
    return f"Signed in as **{tid}** (via {source})"
