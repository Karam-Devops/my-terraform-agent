# app/tests/conftest.py
"""Test bootstrap for app/tests.

Stubs ``streamlit`` so the UI modules under test (``app.ui.sidebar``,
``app.ui.error_surface``, ``app.middleware``) can be imported without
the streamlit package installed -- the package lives in the Cloud Run
container per the Dockerfile, but isn't a hard requirement of the
local dev environment.

The stub is a small class rather than a bare ``MagicMock`` so we can
preserve THREE specific behaviours the production code relies on:

  1. ``st.cache_data(...)`` must work as a real decorator at module
     import time -- otherwise functions decorated with it become
     MagicMock objects and tests can't reach the underlying logic.
     We replace it with a no-op decorator.

  2. ``st.session_state`` must RAISE on access. The middleware uses
     ``try: import streamlit; touch session_state; except Exception:
     fall back to module session``. With a MagicMock that quietly
     succeeds, middleware would use the streamlit branch (with mock
     state that doesn't behave like the real thing) and break the
     existing middleware tests. Raising mimics "outside a Streamlit
     ScriptRunContext" so the existing test_middleware.py fallback
     path still fires.

  3. Display widgets (``st.error``, ``st.info``, ``st.expander``,
     etc.) must be settable / patchable. We create them lazily as
     MagicMock attributes via ``__getattr__``, cached on the instance,
     so ``patch.object(error_surface.st, "error")`` can replace them.

Why per-test conftest rather than a global one: the engine modules
(importer / translator / detector / policy) and ``common/`` don't
need streamlit. Scoping the stub to ``app/tests/`` avoids polluting
their import environments with a fake module they shouldn't see.
"""

from __future__ import annotations

import sys
from unittest.mock import MagicMock


def _noop_cache_decorator(*_args, **_kwargs):
    """Replacement for ``st.cache_data(ttl=...)`` that returns a real
    no-op decorator, leaving the wrapped function callable AND
    attaching ``__wrapped__`` so tests can mirror the real cache's
    introspection contract."""
    def decorator(fn):
        fn.__wrapped__ = fn
        return fn
    return decorator


class _StreamlitStub:
    """Minimal streamlit replacement for tests.

    Class attributes (cache_data / cache_resource) win over
    ``__getattr__`` so they hit before the lazy-MagicMock path.
    Everything else (display widgets) is created on first access
    and cached on the instance.
    """

    cache_data = staticmethod(_noop_cache_decorator)
    cache_resource = staticmethod(_noop_cache_decorator)

    def __getattr__(self, name):
        if name == "session_state":
            # Middleware's streamlit-detect catches Exception and falls
            # back to its module-level WorkdirSession. Raising here
            # keeps the existing test_middleware.py tests working
            # (they assert on the module-fallback path).
            raise RuntimeError(
                "st.session_state requires a Streamlit script run "
                "context (app/tests stub raises so middleware uses "
                "its module-level fallback)"
            )
        m = MagicMock(name=f"streamlit.{name}")
        # Cache on the instance: subsequent accesses return the same
        # mock, matching the real attribute identity contract that
        # patch.object relies on.
        setattr(self, name, m)
        return m


def _install_streamlit_stub() -> None:
    """Inject a streamlit stub into ``sys.modules`` if the real package
    isn't available. Idempotent -- safe across test files."""
    if "streamlit" in sys.modules:
        return
    sys.modules["streamlit"] = _StreamlitStub()


_install_streamlit_stub()
