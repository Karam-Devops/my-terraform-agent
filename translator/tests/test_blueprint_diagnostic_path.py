# translator/tests/test_blueprint_diagnostic_path.py
"""P3-3 unit tests for the per-invocation UUID-suffixed diagnostic-YAML
filename helper.

Pre-P3-3 the diagnostic blueprint was written to a fixed-name path
that two concurrent translations of the same source file would
clobber. This file pins the new contract: every invocation produces
a unique path.

Same import-isolation trick as test_output_path.py: load
yaml_engine via importlib + stub out heavy submodules
(langchain_core, the .. llm_provider) so we can test the pure
helper without dragging in Vertex AI / litellm at import time.
"""

from __future__ import annotations

import importlib.util
import os
import re
import sys
import types
import unittest


PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))


# yaml_engine.py uses `from .. import llm_provider` -- a relative import
# that crosses ONE MORE package boundary than translator/run.py's
# `from . import ...` pattern handled in test_output_path.py. To make
# the relative import resolve, we need a SYNTHETIC PARENT PACKAGE
# (`_p33_parent`) with `translator` registered as a sub-package and
# `llm_provider` registered as a sibling module. Loaded module gets
# `__package__ = "_p33_parent.translator"` so `..` resolves to
# `_p33_parent`, where the stub `llm_provider` lives.
_PARENT_PKG = "_p33_parent"
_TRANSLATOR_PKG = f"{_PARENT_PKG}.translator"
_YAML_ENGINE_MOD = f"{_TRANSLATOR_PKG}.yaml_engine"


def _load_yaml_engine():
    """Load translator.yaml_engine without dragging in the full package's
    heavy deps (langchain, llm_provider, vertexai).

    Test-isolation strategy: synthetic package hierarchy
        _p33_parent (synthetic, holds llm_provider stub)
            \\-- translator (synthetic, holds yaml_engine)
                  \\-- yaml_engine (loaded from real file)

    Yaml_engine's `from .. import llm_provider` resolves up ONE level
    to `_p33_parent.llm_provider`, which we've stubbed.

    Caching note: a sibling test file (test_output_path.py) stubs
    `translator.yaml_engine` for ITS isolation. We use a DIFFERENT
    fully-qualified name (`_p33_parent.translator.yaml_engine`) so the
    two tests' stubs / loads don't interfere with each other.
    """
    cached = sys.modules.get(_YAML_ENGINE_MOD)
    if cached is not None and hasattr(cached, "_blueprint_diagnostic_path"):
        return cached

    # Synthetic parent package -- holds the llm_provider stub.
    if _PARENT_PKG not in sys.modules:
        parent = types.ModuleType(_PARENT_PKG)
        parent.__path__ = [PROJECT_ROOT]
        sys.modules[_PARENT_PKG] = parent

    # Stub llm_provider as a sibling of the translator sub-package.
    llm_provider_name = f"{_PARENT_PKG}.llm_provider"
    if llm_provider_name not in sys.modules:
        sys.modules[llm_provider_name] = types.ModuleType(llm_provider_name)

    # Synthetic translator sub-package.
    if _TRANSLATOR_PKG not in sys.modules:
        sub_pkg = types.ModuleType(_TRANSLATOR_PKG)
        sub_pkg.__path__ = [os.path.join(PROJECT_ROOT, "translator")]
        sub_pkg.__package__ = _PARENT_PKG
        sys.modules[_TRANSLATOR_PKG] = sub_pkg

    # Stub langchain_core for the SystemMessage / HumanMessage imports
    # at the top of yaml_engine.py (we don't call them in the helper
    # under test).
    if "langchain_core" not in sys.modules:
        sys.modules["langchain_core"] = types.ModuleType("langchain_core")
    if "langchain_core.messages" not in sys.modules:
        msg_stub = types.ModuleType("langchain_core.messages")
        msg_stub.SystemMessage = type("SystemMessage", (), {})
        msg_stub.HumanMessage = type("HumanMessage", (), {})
        sys.modules["langchain_core.messages"] = msg_stub

    spec = importlib.util.spec_from_file_location(
        _YAML_ENGINE_MOD,
        os.path.join(PROJECT_ROOT, "translator", "yaml_engine.py"),
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules[_YAML_ENGINE_MOD] = mod
    spec.loader.exec_module(mod)
    return mod


class BlueprintDiagnosticPathTests(unittest.TestCase):

    def setUp(self):
        self.engine = _load_yaml_engine()

    def test_returns_path_under_source_directory(self):
        """Blueprint sits next to the source .tf, not in some random tempdir."""
        out = self.engine._blueprint_diagnostic_path(
            "imported/dev-proj-470211/google_compute_instance_poc_vm.tf"
        )
        norm = out.replace("\\", "/")
        self.assertTrue(
            norm.startswith("imported/dev-proj-470211/_intermediate_blueprint_"),
            f"unexpected path prefix: {norm}",
        )
        self.assertTrue(norm.endswith(".yaml"), f"missing .yaml extension: {norm}")

    def test_filename_strips_google_prefix_and_extension(self):
        """The cleaned basename matches the importer's `google_` strip
        convention so blueprint filenames are easier to match against
        their source files."""
        out = self.engine._blueprint_diagnostic_path(
            "/abs/path/google_storage_bucket.tf"
        )
        # Should NOT contain `google_` and should NOT contain `.tf` in
        # the blueprint filename component.
        base = os.path.basename(out)
        self.assertFalse(
            base.startswith("_intermediate_blueprint_google_"),
            f"google_ prefix not stripped: {base}",
        )
        self.assertNotIn(".tf_", base, f".tf leaked into name: {base}")

    def test_two_calls_produce_different_paths(self):
        """The whole point of P3-3: concurrent translations must not
        collide. Two consecutive calls for the SAME source file MUST
        return distinct paths thanks to the UUID suffix."""
        src = "imported/p/google_compute_instance.tf"
        a = self.engine._blueprint_diagnostic_path(src)
        b = self.engine._blueprint_diagnostic_path(src)
        self.assertNotEqual(
            a, b,
            "two calls for the same source produced the same path -- "
            "concurrent translations would clobber each other",
        )

    def test_uuid_suffix_is_8_hex_chars(self):
        """Pin the suffix shape (8-char hex) so future maintainers don't
        unintentionally shorten or lengthen it."""
        out = self.engine._blueprint_diagnostic_path(
            "imported/p/google_compute_disk.tf"
        )
        # Filename pattern: _intermediate_blueprint_<base>_<uuid8>.yaml
        # Match the trailing _<8 hex chars>.yaml
        base = os.path.basename(out)
        m = re.search(r"_([0-9a-f]{8})\.yaml$", base)
        self.assertIsNotNone(
            m,
            f"filename does not end with _<8-hex>.yaml: {base!r}",
        )

    def test_bare_basename_source_uses_current_dir(self):
        """No directory component in the source -> output uses CWD,
        not a stray empty-string-prefixed path. Same `or "."` guard
        as resolve_output_path."""
        out = self.engine._blueprint_diagnostic_path("google_iam.tf")
        norm = out.replace("\\", "/")
        self.assertTrue(
            norm.startswith("./_intermediate_blueprint_iam_"),
            f"bare-basename source produced unexpected path: {norm}",
        )

    def test_absolute_source_path_preserves_absoluteness(self):
        """Absolute source -> absolute blueprint path under the same
        parent directory. Cleaned basename is `compute_disk` (only the
        leading `google_` is stripped, not the underscored words after)."""
        out = self.engine._blueprint_diagnostic_path(
            "/var/lib/iac/imported/p/google_compute_disk.tf"
        )
        norm = out.replace("\\", "/")
        self.assertTrue(
            norm.startswith(
                "/var/lib/iac/imported/p/_intermediate_blueprint_compute_disk_"
            ),
            f"absolute parent not preserved: {norm}",
        )

    def test_helper_is_pure_no_filesystem_writes(self):
        """The helper must not create directories or files itself --
        that's the caller's job. Verify by passing a path under a
        directory that doesn't exist and confirming no FileNotFoundError
        or directory creation happens."""
        bogus = "definitely_does_not_exist_dir_xyz_p33/google_foo.tf"
        try:
            out = self.engine._blueprint_diagnostic_path(bogus)
        except OSError:
            self.fail("_blueprint_diagnostic_path should not touch the filesystem")
        # Bogus parent dir should NOT have been created.
        self.assertFalse(
            os.path.exists("definitely_does_not_exist_dir_xyz_p33"),
            "_blueprint_diagnostic_path created a directory; it must be a pure helper",
        )

    def test_filename_matches_gitignore_pattern(self):
        """The .gitignore has `_intermediate_blueprint_*.yaml` -- the new
        UUID-suffixed filename must still match this pattern so blueprints
        stay out of git history."""
        out = self.engine._blueprint_diagnostic_path(
            "imported/p/google_compute_instance.tf"
        )
        base = os.path.basename(out)
        # The gitignore glob `_intermediate_blueprint_*.yaml` matches any
        # name starting with that prefix and ending with .yaml. Verify both.
        self.assertTrue(
            base.startswith("_intermediate_blueprint_"),
            f"filename doesn't match gitignore prefix: {base}",
        )
        self.assertTrue(
            base.endswith(".yaml"),
            f"filename doesn't match gitignore suffix: {base}",
        )


if __name__ == "__main__":
    unittest.main()
