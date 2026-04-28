# importer/tests/conftest.py
"""Test bootstrap for importer/tests.

Stubs google-cloud-asset SDK so importer/_asset_client.py imports
cleanly without the real package installed locally. The SDK IS in
requirements.txt and present in the Cloud Run container, but isn't
a hard requirement for unit tests that mock the actual SDK calls.

Same scoping rationale as common/tests/conftest.py and
app/tests/conftest.py -- per-package conftest avoids polluting test
environments for packages that don't need the stub.
"""

from __future__ import annotations

import sys
import types
from unittest.mock import MagicMock


def _install_google_cloud_asset_stub() -> None:
    """Inject minimal google.cloud.asset_v1 stub if the real SDK
    isn't installed. Idempotent across test files."""
    if "google.cloud.asset_v1" in sys.modules:
        return

    # Namespace packages google -> google.cloud (may already exist
    # from common/tests/conftest.py if that ran first in the session).
    if "google" not in sys.modules:
        sys.modules["google"] = types.ModuleType("google")
    if "google.cloud" not in sys.modules:
        sys.modules["google.cloud"] = types.ModuleType("google.cloud")

    # google.cloud.asset_v1 stub. Tests patch _get_asset_client to
    # inject MagicMock clients; the real AssetServiceClient is never
    # instantiated.
    asset_stub = types.ModuleType("google.cloud.asset_v1")

    class _StubAssetServiceClient:
        def __init__(self, *_args, **_kwargs):
            pass

    class _StubContentType:
        RESOURCE = "RESOURCE"  # mirror enum value

    asset_stub.AssetServiceClient = _StubAssetServiceClient
    asset_stub.ContentType = _StubContentType
    sys.modules["google.cloud.asset_v1"] = asset_stub

    # google.api_core.exceptions stub (in case common/tests/conftest.py
    # didn't run first -- pytest's collection order can vary).
    if "google.api_core" not in sys.modules:
        sys.modules["google.api_core"] = types.ModuleType("google.api_core")
    if "google.api_core.exceptions" not in sys.modules:
        exc_stub = types.ModuleType("google.api_core.exceptions")

        class NotFound(Exception):
            pass

        class Forbidden(Exception):
            pass

        class PermissionDenied(Exception):
            pass

        exc_stub.NotFound = NotFound
        exc_stub.Forbidden = Forbidden
        exc_stub.PermissionDenied = PermissionDenied
        sys.modules["google.api_core.exceptions"] = exc_stub

    # google.protobuf.json_format stub for MessageToDict.
    # _asset_to_legacy_dict in _asset_client.py uses this; tests that
    # exercise that path can override the behavior; default just returns
    # the input as-is.
    if "google.protobuf" not in sys.modules:
        sys.modules["google.protobuf"] = types.ModuleType("google.protobuf")
    if "google.protobuf.json_format" not in sys.modules:
        jf_stub = types.ModuleType("google.protobuf.json_format")

        def _stub_message_to_dict(msg, **_kwargs):
            # Tests that need real conversion patch this; default returns
            # an empty dict so _asset_to_legacy_dict's logic doesn't
            # crash on a non-dict result.
            if isinstance(msg, dict):
                return msg
            return {}

        jf_stub.MessageToDict = _stub_message_to_dict
        sys.modules["google.protobuf.json_format"] = jf_stub


_install_google_cloud_asset_stub()
