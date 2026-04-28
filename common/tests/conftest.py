# common/tests/conftest.py
"""Test bootstrap for common/tests.

Stubs the google-cloud-storage SDK so common/storage.py and
common/snapshots.py can be imported without the real package
installed locally. The SDK IS in requirements.txt and present in
the Cloud Run container, but isn't a hard requirement for unit tests
that mock the actual GCS calls anyway.

The stub is minimal:
  * ``google`` and ``google.cloud`` namespace packages
  * ``google.cloud.storage`` with a ``Client`` placeholder (tests
    patch ``_get_gcs_client`` to return a MagicMock so the real
    Client is never invoked)
  * ``google.api_core.exceptions`` with a ``NotFound`` exception
    class -- tests use this to simulate "missing prefix" failures

Why scoped to common/tests/ rather than global: the engine modules
(importer / translator / detector / policy) and ``app/`` don't
need this stub. Scoping prevents accidental cross-contamination
between test packages.
"""

from __future__ import annotations

import sys
import types


def _install_google_cloud_stub() -> None:
    """Inject a minimal google.cloud.storage stub if the real SDK
    isn't installed. Idempotent across test files."""
    if "google.cloud.storage" in sys.modules:
        return

    # Create the namespace packages google -> google.cloud.
    if "google" not in sys.modules:
        sys.modules["google"] = types.ModuleType("google")
    if "google.cloud" not in sys.modules:
        sys.modules["google.cloud"] = types.ModuleType("google.cloud")

    # google.cloud.storage stub. Tests patch _get_gcs_client to inject
    # a MagicMock client; the real Client class is never instantiated.
    storage_stub = types.ModuleType("google.cloud.storage")

    class _StubClient:
        """Placeholder Client. Real instantiation never happens in
        tests because they patch _get_gcs_client at the module level."""
        def __init__(self, *_args, **_kwargs):
            pass

    class _StubBlob:
        pass

    storage_stub.Client = _StubClient
    storage_stub.Blob = _StubBlob
    sys.modules["google.cloud.storage"] = storage_stub

    # google.api_core.exceptions stub. Tests use NotFound to simulate
    # the "missing prefix" branch of hydrate_workdir / read_latest_snapshot.
    if "google.api_core" not in sys.modules:
        sys.modules["google.api_core"] = types.ModuleType("google.api_core")
    exc_stub = types.ModuleType("google.api_core.exceptions")

    class NotFound(Exception):
        """Mirror of google.api_core.exceptions.NotFound for tests."""

    class Forbidden(Exception):
        """Mirror of google.api_core.exceptions.Forbidden for tests."""

    exc_stub.NotFound = NotFound
    exc_stub.Forbidden = Forbidden
    sys.modules["google.api_core.exceptions"] = exc_stub


_install_google_cloud_stub()
