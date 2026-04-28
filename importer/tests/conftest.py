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

    # PERF-T0b: per-service SDK stubs. Tests for _describe_router patch
    # _get_storage_client / _get_compute_instances_client /
    # _get_container_clusters_client to inject MagicMock clients, so
    # the real SDK classes are never instantiated. The stubs just need
    # to be importable.
    if "google.cloud.storage" not in sys.modules:
        storage_stub = types.ModuleType("google.cloud.storage")
        storage_stub.Client = MagicMock(name="storage.Client")
        sys.modules["google.cloud.storage"] = storage_stub

    if "google.cloud.compute_v1" not in sys.modules:
        compute_stub = types.ModuleType("google.cloud.compute_v1")
        # All compute clients we use (v1 + v2 added in PERF-T0b v2)
        for cls_name in (
            "InstancesClient", "DisksClient", "FirewallsClient",
            "NetworksClient", "SubnetworksClient", "AddressesClient",
            "GlobalAddressesClient", "InstanceTemplatesClient",
        ):
            setattr(compute_stub, cls_name,
                    MagicMock(name=f"compute.{cls_name}"))
        sys.modules["google.cloud.compute_v1"] = compute_stub

    if "google.cloud.container_v1" not in sys.modules:
        container_stub = types.ModuleType("google.cloud.container_v1")
        container_stub.ClusterManagerClient = MagicMock(
            name="container.ClusterManagerClient",
        )
        sys.modules["google.cloud.container_v1"] = container_stub

    # PERF-T0b v2 SDK stubs
    if "google.cloud.kms_v1" not in sys.modules:
        kms_stub = types.ModuleType("google.cloud.kms_v1")
        kms_stub.KeyManagementServiceClient = MagicMock(
            name="kms.KeyManagementServiceClient",
        )
        sys.modules["google.cloud.kms_v1"] = kms_stub

    if "google.cloud.pubsub_v1" not in sys.modules:
        pubsub_stub = types.ModuleType("google.cloud.pubsub_v1")
        pubsub_stub.PublisherClient = MagicMock(name="pubsub.PublisherClient")
        pubsub_stub.SubscriberClient = MagicMock(name="pubsub.SubscriberClient")
        sys.modules["google.cloud.pubsub_v1"] = pubsub_stub

    if "google.cloud.iam_admin_v1" not in sys.modules:
        iam_stub = types.ModuleType("google.cloud.iam_admin_v1")
        iam_stub.IAMClient = MagicMock(name="iam.IAMClient")
        sys.modules["google.cloud.iam_admin_v1"] = iam_stub

    # google-cloud-run was dropped due to protobuf version conflict --
    # _describe_cloud_run_v2_service uses googleapiclient instead.

    # googleapiclient (for Cloud SQL Admin AND Cloud Run v2 -- both use
    # discovery-based clients now after dropping google-cloud-run)
    if "googleapiclient" not in sys.modules:
        sys.modules["googleapiclient"] = types.ModuleType("googleapiclient")
    if "googleapiclient.discovery" not in sys.modules:
        disc_stub = types.ModuleType("googleapiclient.discovery")
        disc_stub.build = MagicMock(name="googleapiclient.discovery.build")
        sys.modules["googleapiclient.discovery"] = disc_stub

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
