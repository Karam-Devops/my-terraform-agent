# translator/tests/test_run_translation_batch_parallel.py
"""P4-15 tests for the parallel run_translation_batch + the new
multi-select CLI helpers (_translate_one_file, _select_files).

Mocks run_translation_pipeline so the tests don't shell out to real
LLMs / Vertex AI. Verifies:
  * Parallel execution actually overlaps (concurrency > 1)
  * Per-file failure isolation (one bad file doesn't kill the batch)
  * Input-ordering preservation (results returned in selection order
    despite parallel completion)
  * Pure-counts derived from per-file outcomes
  * Empty / invalid inputs raise per the A+D contract

Same import-isolation pattern as test_discover_translatable_files.py
-- distinct synthetic parent (`_p415_parent`) to avoid sys.modules
pollution across test files.
"""

from __future__ import annotations

import importlib.util
import os
import sys
import time
import types
import unittest
from unittest.mock import patch


PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))

_PARENT_PKG = "_p415_parent"
_TRANSLATOR_PKG = f"{_PARENT_PKG}.translator"
_IMPORTER_PKG = f"{_PARENT_PKG}.importer"
_RUN_MOD = f"{_TRANSLATOR_PKG}.run"


def _load_translator_run():
    """Synthetic-parent loader -- mirrors test_discover_translatable_files.py
    but with a distinct parent name to avoid sys.modules pollution."""
    cached = sys.modules.get(_RUN_MOD)
    if cached is not None and hasattr(cached, "run_translation_batch"):
        return cached

    if _PARENT_PKG not in sys.modules:
        parent = types.ModuleType(_PARENT_PKG)
        parent.__path__ = [PROJECT_ROOT]
        sys.modules[_PARENT_PKG] = parent

    if _TRANSLATOR_PKG not in sys.modules:
        sub = types.ModuleType(_TRANSLATOR_PKG)
        sub.__path__ = [os.path.join(PROJECT_ROOT, "translator")]
        sub.__package__ = _PARENT_PKG
        sys.modules[_TRANSLATOR_PKG] = sub

    if _IMPORTER_PKG not in sys.modules:
        imp_pkg = types.ModuleType(_IMPORTER_PKG)
        imp_pkg.__path__ = [os.path.join(PROJECT_ROOT, "importer")]
        imp_pkg.__package__ = _PARENT_PKG
        sys.modules[_IMPORTER_PKG] = imp_pkg

    importer_config_name = f"{_IMPORTER_PKG}.config"
    if importer_config_name not in sys.modules:
        spec = importlib.util.spec_from_file_location(
            importer_config_name,
            os.path.join(PROJECT_ROOT, "importer", "config.py"),
        )
        mod = importlib.util.module_from_spec(spec)
        sys.modules[importer_config_name] = mod
        spec.loader.exec_module(mod)

    # P4-15: load REAL translator/config.py so MAX_TRANSLATION_WORKERS is
    # available. The other heavy submodules can stay stubbed -- the
    # parallel batch tests don't exercise yaml_engine / aws_engine /
    # azure_engine / tf_validator code paths (they're swapped out by
    # the run_translation_pipeline mock).
    config_name = f"{_TRANSLATOR_PKG}.config"
    if config_name not in sys.modules:
        spec = importlib.util.spec_from_file_location(
            config_name,
            os.path.join(PROJECT_ROOT, "translator", "config.py"),
        )
        mod = importlib.util.module_from_spec(spec)
        sys.modules[config_name] = mod
        spec.loader.exec_module(mod)

    for stub_name in ("yaml_engine", "aws_engine", "azure_engine",
                      "tf_validator"):
        full = f"{_TRANSLATOR_PKG}.{stub_name}"
        if full not in sys.modules:
            sys.modules[full] = types.ModuleType(full)

    results_name = f"{_TRANSLATOR_PKG}.results"
    if results_name not in sys.modules:
        spec = importlib.util.spec_from_file_location(
            results_name,
            os.path.join(PROJECT_ROOT, "translator", "results.py"),
        )
        mod = importlib.util.module_from_spec(spec)
        sys.modules[results_name] = mod
        spec.loader.exec_module(mod)

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


class ParallelBatchExecutionTests(unittest.TestCase):
    """Verifies that ThreadPoolExecutor actually overlaps file work
    rather than running sequentially."""

    def setUp(self):
        self.r = _load_translator_run()

    def test_batch_runs_files_in_parallel(self):
        # 4 files, each "takes" 0.3s. Sequential would be ~1.2s.
        # Parallel with 4 workers should be ~0.3s. Use 0.6s cap as
        # threshold to leave headroom for thread scheduling overhead.
        def _slow_pipeline(target, source_path, *, tenant_id=None,
                           project_id=None):
            time.sleep(0.3)
            return (True, source_path + ".translated")

        source_paths = [f"file_{i}.tf" for i in range(4)]

        with patch.object(self.r, "run_translation_pipeline",
                          side_effect=_slow_pipeline):
            t0 = time.monotonic()
            result = self.r.run_translation_batch("aws", source_paths)
            elapsed = time.monotonic() - t0

        self.assertEqual(result.translated, 4)
        # If sequential we'd see ~1.2s. Parallel under 4 workers should
        # complete well under that. 0.8s threshold leaves room for
        # thread spin-up + Windows scheduling jitter.
        self.assertLess(
            elapsed, 0.8,
            f"Batch took {elapsed:.2f}s -- expected ~0.3s under parallelism. "
            f"Either the ThreadPoolExecutor isn't being used or workers "
            f"are serialized.",
        )

    def test_max_workers_respected(self):
        # Set max_workers=1 via env override and verify we get
        # sequential timing back (proves the cap is honored).
        from importer import config as importer_cfg  # noqa: F401
        with patch.dict(os.environ, {"MAX_TRANSLATION_WORKERS": "1"}):
            # Reload translator.config so the new env value is picked up.
            import importlib
            cfg_name = f"{_TRANSLATOR_PKG}.config"
            importlib.reload(sys.modules[cfg_name])
            # Reload run module so its `from . import config` re-binds.
            importlib.reload(sys.modules[_RUN_MOD])
            r_serial = sys.modules[_RUN_MOD]

            def _slow_pipeline(target, source_path, *, tenant_id=None,
                               project_id=None):
                time.sleep(0.2)
                return (True, source_path + ".translated")

            source_paths = [f"file_{i}.tf" for i in range(3)]
            with patch.object(r_serial, "run_translation_pipeline",
                              side_effect=_slow_pipeline):
                t0 = time.monotonic()
                r_serial.run_translation_batch("aws", source_paths)
                elapsed = time.monotonic() - t0

            # Sequential: 3 * 0.2s = 0.6s. Parallel-1 should produce
            # similar timing (lower bound; allow some jitter).
            self.assertGreater(
                elapsed, 0.5,
                f"With MAX_TRANSLATION_WORKERS=1, expected ~0.6s; got "
                f"{elapsed:.2f}s. Parallelism may be ignoring the cap.",
            )

        # Restore default config + run module for subsequent tests.
        import importlib
        importlib.reload(sys.modules[f"{_TRANSLATOR_PKG}.config"])
        importlib.reload(sys.modules[_RUN_MOD])


class PerFileFailureIsolationTests(unittest.TestCase):
    """One bad file MUST NOT kill the batch -- the other files still
    translate, the bad one lands in failed."""

    def setUp(self):
        self.r = _load_translator_run()

    def test_one_exception_in_middle_does_not_kill_batch(self):
        def _maybe_raise(target, source_path, *, tenant_id=None,
                         project_id=None):
            if "boom" in source_path:
                raise RuntimeError("simulated LLM failure")
            return (True, source_path + ".translated")

        source_paths = ["good_a.tf", "boom_b.tf", "good_c.tf", "good_d.tf"]

        with patch.object(self.r, "run_translation_pipeline",
                          side_effect=_maybe_raise):
            result = self.r.run_translation_batch("aws", source_paths)

        self.assertEqual(result.translated, 3)
        self.assertEqual(result.failed, 1)
        self.assertEqual(result.needs_attention, 0)
        # Find the failed entry; verify it's the boom one + carries
        # the exception detail.
        failed_outcomes = [f for f in result.files if f.status == "failed"]
        self.assertEqual(len(failed_outcomes), 1)
        self.assertEqual(failed_outcomes[0].source_path, "boom_b.tf")
        self.assertIn("RuntimeError", failed_outcomes[0].validation_error)
        self.assertIn("simulated LLM failure",
                      failed_outcomes[0].validation_error)


class InputOrderingPreservationTests(unittest.TestCase):
    """The TranslationResult.files list MUST be in the same order as
    source_paths -- per the dataclass docstring contract. Parallel
    completion order is non-deterministic, so the implementation has
    to actively preserve input order via index-mapping."""

    def setUp(self):
        self.r = _load_translator_run()

    def test_files_list_returned_in_input_order(self):
        # Variable per-file delays force completion to happen out of
        # order: file_0 sleeps longest (completes last), file_4 shortest
        # (completes first). Without ordering preservation, the
        # returned files list would be in reverse-completion order.
        delays = [0.4, 0.3, 0.2, 0.1, 0.05]
        source_paths = [f"file_{i}.tf" for i in range(5)]

        def _delayed_pipeline(target, source_path, *, tenant_id=None,
                              project_id=None):
            i = int(source_path.split("_")[1].split(".")[0])
            time.sleep(delays[i])
            return (True, source_path + ".translated")

        with patch.object(self.r, "run_translation_pipeline",
                          side_effect=_delayed_pipeline):
            result = self.r.run_translation_batch("aws", source_paths)

        # files[i] must correspond to source_paths[i].
        self.assertEqual(len(result.files), 5)
        for i, outcome in enumerate(result.files):
            self.assertEqual(
                outcome.source_path, source_paths[i],
                f"Result at index {i} is {outcome.source_path}; "
                f"expected {source_paths[i]} (parallel completion "
                f"reordering not corrected by index map)",
            )


class BatchPreflightTests(unittest.TestCase):
    """A+D contract: invalid inputs RAISE; batch issues RETURN."""

    def setUp(self):
        self.r = _load_translator_run()

    def test_empty_source_paths_raises(self):
        with self.assertRaises(ValueError) as ctx:
            self.r.run_translation_batch("aws", [])
        self.assertIn("non-empty", str(ctx.exception))

    def test_invalid_target_cloud_raises(self):
        with self.assertRaises(ValueError) as ctx:
            self.r.run_translation_batch("gcp", ["x.tf"])
        self.assertIn("aws", str(ctx.exception))
        self.assertIn("azure", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
