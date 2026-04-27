# translator/tests/test_output_path.py
"""
Smoke tests for TODO #13 — `resolve_output_path()` puts translated files
into a per-target subdirectory of the source dir, instead of jumbling
them next to the GCP originals.

We test the pure helper rather than the full pipeline so we don't have
to mock the engine, validator, and LLM. The pipeline integration is
verified by the user's batch test on real translations.
"""

from __future__ import annotations

import importlib.util
import os
import sys
import types
import unittest


PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))


# P3-6 update: translator/run.py now does
# `from ..importer.config import TF_TYPE_TO_GCLOUD_INFO`. This loader
# previously set `translator` up as a top-level package (`__package__
# = "translator"`); the relative-up import would then fail with
# "attempted relative import beyond top-level package" because there's
# no parent. Synthetic parent package (`_top_path_parent`) holds both
# `translator` and `importer` as sub-packages so the relative import
# resolves. Distinct package name from the other test files'
# synthetic-parent names (`_p33_parent`, `_p35_parent`, `_p36_parent`)
# to avoid cross-test sys.modules pollution.
_PARENT_PKG = "_top_path_parent"
_TRANSLATOR_PKG = f"{_PARENT_PKG}.translator"
_IMPORTER_PKG = f"{_PARENT_PKG}.importer"
_RUN_MOD = f"{_TRANSLATOR_PKG}.run"


def _load_translator_run():
    """Load translator.run without dragging in the full package's heavy
    deps (yaml_engine pulls in litellm, etc.). We only need the pure
    `resolve_output_path` helper which has no runtime dependencies.

    P3-6 added a `from ..importer.config import` to run.py, so the
    loader now also stubs a synthetic parent + real importer.config
    (pure dicts, no I/O, safe to load) so the relative import resolves.
    """
    cached = sys.modules.get(_RUN_MOD)
    if cached is not None and hasattr(cached, "resolve_output_path"):
        return cached

    # Synthetic parent package.
    if _PARENT_PKG not in sys.modules:
        parent = types.ModuleType(_PARENT_PKG)
        parent.__path__ = [PROJECT_ROOT]
        sys.modules[_PARENT_PKG] = parent

    # Synthetic translator sub-package (run.py loads into here).
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

    # Real importer.config -- pure dicts, no I/O.
    importer_config_name = f"{_IMPORTER_PKG}.config"
    if importer_config_name not in sys.modules:
        spec = importlib.util.spec_from_file_location(
            importer_config_name,
            os.path.join(PROJECT_ROOT, "importer", "config.py"),
        )
        mod = importlib.util.module_from_spec(spec)
        sys.modules[importer_config_name] = mod
        spec.loader.exec_module(mod)

    # Stub heavy translator submodules. The helper under test doesn't
    # touch any of them, so ModuleType placeholders suffice.
    for stub_name in ("config", "yaml_engine", "aws_engine", "azure_engine",
                      "tf_validator"):
        full = f"{_TRANSLATOR_PKG}.{stub_name}"
        if full not in sys.modules:
            sys.modules[full] = types.ModuleType(full)

    # Real results.py -- dependency-free dataclasses; needed for the
    # `from .results import FileOutcome, TranslationResult` in run.py.
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


class ResolveOutputPathTests(unittest.TestCase):
    def setUp(self):
        self.r = _load_translator_run()

    def test_aws_translation_lands_in_aws_subdir(self):
        out = self.r.resolve_output_path(
            "generated_iac/google_compute_instance.tf",
            target="aws",
            prefix="aws",
        )
        # Normalise separators for cross-platform comparison.
        norm = out.replace("\\", "/")
        self.assertEqual(
            norm,
            "generated_iac/translated/aws/aws_translated_compute_instance.tf",
        )

    def test_azure_translation_lands_in_azure_subdir(self):
        out = self.r.resolve_output_path(
            "generated_iac/google_storage_bucket.tf",
            target="azure",
            prefix="azure",
        )
        norm = out.replace("\\", "/")
        self.assertEqual(
            norm,
            "generated_iac/translated/azure/azure_translated_storage_bucket.tf",
        )

    def test_google_prefix_is_stripped_from_filename(self):
        # Source file with `google_` prefix should drop it in the output
        # name (otherwise the AWS file would be named
        # aws_translated_google_compute_instance.tf, which is silly).
        out = self.r.resolve_output_path(
            "/abs/path/google_pubsub_topic.tf",
            target="aws",
            prefix="aws",
        )
        self.assertTrue(out.endswith("aws_translated_pubsub_topic.tf"),
                        f"unexpected output filename: {out}")

    def test_source_without_google_prefix_passes_through(self):
        # Not every source file uses the `google_` prefix (custom files,
        # composite stacks). The helper just leaves them alone.
        out = self.r.resolve_output_path(
            "stacks/my_custom.tf",
            target="aws",
            prefix="aws",
        )
        self.assertTrue(out.endswith("aws_translated_my_custom.tf"))

    def test_bare_basename_source_uses_current_dir(self):
        # If source is just a filename with no directory component,
        # output should land in `./translated/<target>/`. Without the
        # `or "."` guard this would silently emit `translated/aws/...`
        # without the leading dot, which works on POSIX but is
        # technically wrong.
        out = self.r.resolve_output_path(
            "google_iam.tf",
            target="aws",
            prefix="aws",
        )
        norm = out.replace("\\", "/")
        self.assertEqual(norm, "./translated/aws/aws_translated_iam.tf")

    def test_absolute_source_path_preserves_absoluteness(self):
        out = self.r.resolve_output_path(
            "/var/lib/iac/generated_iac/google_compute_disk.tf",
            target="aws",
            prefix="aws",
        )
        norm = out.replace("\\", "/")
        # The returned path stays under the same absolute parent — we
        # don't rewrite to a magic "global" output dir.
        self.assertTrue(
            norm.startswith("/var/lib/iac/generated_iac/translated/aws/"),
            f"absolute parent not preserved: {norm}",
        )

    def test_helper_is_pure_no_filesystem_writes(self):
        # The helper must not create directories itself — that's the
        # caller's job. Verify by passing a path under a directory that
        # doesn't exist and confirming no FileNotFoundError or directory
        # creation happens.
        bogus = "definitely_does_not_exist_dir_xyz/google_foo.tf"
        try:
            out = self.r.resolve_output_path(bogus, target="aws", prefix="aws")
        except OSError:
            self.fail("resolve_output_path should not touch the filesystem")
        # And the bogus parent dir should NOT have been created.
        self.assertFalse(
            os.path.exists("definitely_does_not_exist_dir_xyz"),
            "resolve_output_path created a directory; it must be a pure helper",
        )

    def test_separation_between_aws_and_azure_outputs(self):
        # The whole point of the fix: AWS and Azure translations of the
        # SAME source file must land in different directories so they
        # don't clobber each other.
        src = "generated_iac/google_compute_instance.tf"
        aws_out = self.r.resolve_output_path(src, target="aws", prefix="aws")
        azure_out = self.r.resolve_output_path(src, target="azure", prefix="azure")
        self.assertNotEqual(
            os.path.dirname(aws_out),
            os.path.dirname(azure_out),
            "AWS and Azure translations land in the same dir — fix didn't separate them",
        )


if __name__ == "__main__":
    unittest.main()
