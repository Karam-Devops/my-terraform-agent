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
        import json as _json

        def _stub_message_to_dict(msg, **_kwargs):
            # Tests that need real conversion patch this; default returns
            # the input as-is when it's already a dict (covers the
            # _asset_to_legacy_dict pattern).
            if isinstance(msg, dict):
                return msg
            return {}

        def _stub_message_to_json(msg, **_kwargs):
            # MessageToJson is the bulletproof path used by
            # _struct_to_dict (PUI-1 SMOKE 2026-04-28 fix). For tests,
            # if the input is already a dict (most test fixtures), JSON-
            # serialize it directly. Otherwise return "{}" so the
            # downstream json.loads doesn't crash.
            if isinstance(msg, dict):
                return _json.dumps(msg)
            return "{}"

        jf_stub.MessageToDict = _stub_message_to_dict
        jf_stub.MessageToJson = _stub_message_to_json
        sys.modules["google.protobuf.json_format"] = jf_stub


_install_google_cloud_asset_stub()
