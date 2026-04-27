# translator/tests/test_discover_translatable_files.py
"""P3-6 unit tests for discover_translatable_files() + _human_friendly_type().

Pure-function tests against a tempdir layout (no LLM, no engine).
Same import-isolation as test_output_path.py -- stub the heavy
translator submodules so we can load translator.run without
pulling in vertexai / langchain.
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import sys
import tempfile
import types
import unittest


PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))


# P3-6: translator/run.py now imports `from ..importer.config import
# TF_TYPE_TO_GCLOUD_INFO`. To make that relative import resolve in
# the test loader, we set up a SYNTHETIC PARENT PACKAGE
# (`_p36_parent`) with both `translator` and `importer` as
# sub-packages. Loaded run.py gets `__package__ =
# "_p36_parent.translator"` so `..importer` resolves to
# `_p36_parent.importer`. Distinct package name from
# test_output_path.py's flat `translator` stub avoids cross-test
# sys.modules pollution.
_PARENT_PKG = "_p36_parent"
_TRANSLATOR_PKG = f"{_PARENT_PKG}.translator"
_IMPORTER_PKG = f"{_PARENT_PKG}.importer"
_RUN_MOD = f"{_TRANSLATOR_PKG}.run"


def _load_translator_run():
    """Load translator.run with a synthetic parent so the cross-package
    `from ..importer.config import` resolves at module load time."""
    cached = sys.modules.get(_RUN_MOD)
    if cached is not None and hasattr(cached, "discover_translatable_files"):
        return cached

    # Synthetic parent package.
    if _PARENT_PKG not in sys.modules:
        parent = types.ModuleType(_PARENT_PKG)
        parent.__path__ = [PROJECT_ROOT]
        sys.modules[_PARENT_PKG] = parent

    # Synthetic translator sub-package (where run.py loads into).
    if _TRANSLATOR_PKG not in sys.modules:
        sub = types.ModuleType(_TRANSLATOR_PKG)
        sub.__path__ = [os.path.join(PROJECT_ROOT, "translator")]
        sub.__package__ = _PARENT_PKG
        sys.modules[_TRANSLATOR_PKG] = sub

    # Synthetic importer sub-package (so `..importer.config` resolves).
    if _IMPORTER_PKG not in sys.modules:
        imp_pkg = types.ModuleType(_IMPORTER_PKG)
        imp_pkg.__path__ = [os.path.join(PROJECT_ROOT, "importer")]
        imp_pkg.__package__ = _PARENT_PKG
        sys.modules[_IMPORTER_PKG] = imp_pkg

    # Real importer.config -- pure dicts, no I/O, safe to load.
    importer_config_name = f"{_IMPORTER_PKG}.config"
    if importer_config_name not in sys.modules:
        spec = importlib.util.spec_from_file_location(
            importer_config_name,
            os.path.join(PROJECT_ROOT, "importer", "config.py"),
        )
        mod = importlib.util.module_from_spec(spec)
        sys.modules[importer_config_name] = mod
        spec.loader.exec_module(mod)

    # Stub heavy translator submodules.
    for stub_name in ("config", "yaml_engine", "aws_engine", "azure_engine",
                      "tf_validator"):
        full = f"{_TRANSLATOR_PKG}.{stub_name}"
        if full not in sys.modules:
            sys.modules[full] = types.ModuleType(full)

    # Real results.py -- dependency-free dataclasses.
    results_name = f"{_TRANSLATOR_PKG}.results"
    if results_name not in sys.modules:
        spec = importlib.util.spec_from_file_location(
            results_name,
            os.path.join(PROJECT_ROOT, "translator", "results.py"),
        )
        mod = importlib.util.module_from_spec(spec)
        sys.modules[results_name] = mod
        spec.loader.exec_module(mod)

    # common.logging -- real, dependency-free.
    if "common.logging" not in sys.modules:
        if "common" not in sys.modules:
            common_pkg = types.ModuleType("common")
            common_pkg.__path__ = [os.path.join(PROJECT_ROOT, "common")]
            sys.modules["common"] = common_pkg
        spec = importlib.util.spec_from_file_location(
            "common.logging",
            os.path.join(PROJECT_ROOT, "common", "logging.py"),
        )
        mod = importlib.util.module_from_spec(spec)
        sys.modules["common.logging"] = mod
        spec.loader.exec_module(mod)

    spec = importlib.util.spec_from_file_location(
        _RUN_MOD,
        os.path.join(PROJECT_ROOT, "translator", "run.py"),
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules[_RUN_MOD] = mod
    spec.loader.exec_module(mod)
    return mod


class DiscoverTranslatableFilesTests(unittest.TestCase):

    def setUp(self):
        self.r = _load_translator_run()
        self.tmpdir = tempfile.mkdtemp(prefix="translator_discover_test_")

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _touch(self, name: str) -> str:
        """Create an empty file with the given name in the test tempdir."""
        path = os.path.join(self.tmpdir, name)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("# placeholder\n")
        return path

    def test_empty_workdir_returns_empty_list(self):
        out = self.r.discover_translatable_files(self.tmpdir)
        self.assertEqual(out, [])

    def test_nonexistent_workdir_returns_empty_list(self):
        """No file-not-found exception -- empty list signals 'nothing
        to translate' regardless of cause."""
        bogus = os.path.join(self.tmpdir, "definitely_does_not_exist")
        out = self.r.discover_translatable_files(bogus)
        self.assertEqual(out, [])

    def test_none_workdir_returns_empty_list(self):
        """Defensive: calling code (e.g. UI) might pass None when no
        project is selected yet."""
        self.assertEqual(self.r.discover_translatable_files(None), [])

    def test_extracts_tf_type_and_hcl_name_from_filename(self):
        self._touch("google_compute_instance_poc_vm.tf")
        out = self.r.discover_translatable_files(self.tmpdir)
        self.assertEqual(len(out), 1)
        entry = out[0]
        self.assertEqual(entry["tf_type"], "google_compute_instance")
        self.assertEqual(entry["hcl_name"], "poc_vm")
        self.assertTrue(entry["file_path"].endswith(
            "google_compute_instance_poc_vm.tf",
        ))

    def test_display_label_is_human_friendly(self):
        """Customer-facing label must NOT show the raw tf_type."""
        self._touch("google_compute_instance_poc_vm.tf")
        out = self.r.discover_translatable_files(self.tmpdir)
        self.assertEqual(out[0]["display_label"], "VM · poc-vm")

    def test_display_label_rehumanises_underscores(self):
        """The importer underscored hyphens to make HCL labels valid;
        the customer-facing display restores the hyphens for
        readability."""
        self._touch("google_storage_bucket_poc_smoke_bucket_dev_proj_470211.tf")
        out = self.r.discover_translatable_files(self.tmpdir)
        # Display: "Bucket · poc-smoke-bucket-dev-proj-470211"
        self.assertIn("poc-smoke-bucket-dev-proj-470211",
                      out[0]["display_label"])
        self.assertTrue(out[0]["display_label"].startswith("Bucket · "))

    def test_unknown_tf_type_is_skipped(self):
        """Allowlist-based discovery: tf_types NOT in the importer's
        known map are skipped. This is the correct behavior because:

          1. Custom modules / operator-edited files in the workdir
             could have arbitrary names; processing them through the
             LLM with no schema grounding produces garbage output.
          2. New resource types added to the importer (Phase 2+) flow
             into the translator's discovery automatically via the
             same allowlist (TF_TYPE_TO_GCLOUD_INFO from
             importer/config.py) -- no separate translator config
             update needed.

        If a future test adds `google_compute_router` to the
        importer's map, this test fixture should be updated to use
        a still-unknown name like `google_compute_zzzzznotreal_x.tf`.
        """
        self._touch("google_compute_zzzzznotreal_x_y.tf")
        out = self.r.discover_translatable_files(self.tmpdir)
        self.assertEqual(out, [])

    def test_skips_non_importer_files(self):
        """Files that don't match the `<known_tf_type>_<hcl_name>.tf`
        pattern are silently skipped -- they're operator-edited
        modules, custom artifacts, README backups, etc., none of
        which the translator can safely process."""
        self._touch("google_compute_instance_poc_vm.tf")  # discoverable
        self._touch("README.md")                           # not .tf
        self._touch("notes.tf")                            # no _ separator
        self._touch("custom_module.tf")                    # unknown tf_type
        out = self.r.discover_translatable_files(self.tmpdir)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["hcl_name"], "poc_vm")

    def test_results_are_sorted_by_display_label(self):
        """Stable presentation order in the UI checkbox grid."""
        self._touch("google_storage_bucket_poc_bucket.tf")
        self._touch("google_compute_instance_poc_vm.tf")
        self._touch("google_service_account_poc_sa.tf")
        out = self.r.discover_translatable_files(self.tmpdir)
        labels = [e["display_label"] for e in out]
        self.assertEqual(labels, sorted(labels))


class HumanFriendlyTypeTests(unittest.TestCase):

    def setUp(self):
        self.r = _load_translator_run()

    def test_known_types_get_friendly_labels(self):
        for tf_type, expected in [
            ("google_compute_instance", "VM"),
            ("google_storage_bucket", "Bucket"),
            ("google_kms_crypto_key", "KMS Key"),
            ("google_pubsub_subscription", "Pub/Sub Subscription"),
            ("google_cloud_run_v2_service", "Cloud Run Service"),
        ]:
            with self.subTest(tf_type=tf_type):
                self.assertEqual(self.r._human_friendly_type(tf_type), expected)

    def test_unknown_type_returns_raw_input(self):
        """Forward-compatible: new types added to the importer don't
        break translator discovery -- they just show their raw name."""
        self.assertEqual(
            self.r._human_friendly_type("google_compute_router"),
            "google_compute_router",
        )
        self.assertEqual(
            self.r._human_friendly_type("aws_s3_bucket"),
            "aws_s3_bucket",
        )


if __name__ == "__main__":
    unittest.main()
