# common/tests/test_workdir.py
"""
Unit tests for common.workdir.

Coverage focus is the canonical-lock-file seeding contract -- the rest of
the module (resolve_project_workdir, list_project_workdirs) is exercised
indirectly by the importer + detector smoke paths and is intentionally
not duplicated here.

Why seed_lock_file gets dedicated tests: it's load-bearing for
multi-tenant SaaS. If it ever silently overwrites a workdir's existing
lock file, two clients running side-by-side could end up with each
other's pinned provider versions and `terraform plan` would diverge from
what the dev tested. The tests below pin the three behaviours that
prevent that:
    1. lock present in workdir   -> NEVER overwrite
    2. lock absent + canonical present -> copy in
    3. canonical absent (e.g. unbundled deployment) -> clean fallback
"""

from __future__ import annotations

import os
import tempfile
import unittest
from unittest.mock import patch

from common import workdir as _workdir
from unittest.mock import patch

from common import workdir


class SeedLockFileTests(unittest.TestCase):
    """Pin the seed_lock_file() contract."""

    def setUp(self):
        # Each test runs in an isolated tmp dir so the real repo's
        # provider_versions/ and imported/ are untouched. We patch the
        # module's _repo_root to point at our tmp so canonical_lock_file_path
        # resolves inside the sandbox.
        self._tmpdir = tempfile.TemporaryDirectory()
        self.tmp = self._tmpdir.name

        self.workdir = os.path.join(self.tmp, "imported", "test-proj-001")
        os.makedirs(self.workdir, exist_ok=True)

        self.canonical_dir = os.path.join(self.tmp, "provider_versions")
        os.makedirs(self.canonical_dir, exist_ok=True)
        self.canonical = os.path.join(self.canonical_dir, ".terraform.lock.hcl")

        # Repo-root patch: makes canonical_lock_file_path() resolve into
        # our tmp sandbox instead of the real repo.
        self._patcher = patch.object(workdir, "_repo_root", return_value=self.tmp)
        self._patcher.start()

    def tearDown(self):
        self._patcher.stop()
        self._tmpdir.cleanup()

    # -- behaviour 1: never overwrite an existing lock ---------------------

    def test_existing_lock_is_never_overwritten(self):
        """If the workdir already has a lock file, the seed must NOT touch it.

        The operator's pin wins. Overwriting could silently change which
        provider versions the next `terraform init` resolves -- exactly
        the silent drift the lock file is meant to prevent.
        """
        existing_contents = b"# operator's pinned lock; do not touch\n"
        target = os.path.join(self.workdir, ".terraform.lock.hcl")
        with open(target, "wb") as f:
            f.write(existing_contents)

        with open(self.canonical, "wb") as f:
            f.write(b"# canonical lock with DIFFERENT versions\n")

        result = workdir.seed_lock_file(self.workdir)

        self.assertFalse(result, "no-op should return False")
        with open(target, "rb") as f:
            self.assertEqual(f.read(), existing_contents,
                             "existing lock must be untouched")

    # -- behaviour 2: seed when lock is absent -----------------------------

    def test_absent_lock_gets_seeded_from_canonical(self):
        """Empty workdir + canonical present -> canonical is copied in."""
        canonical_contents = b'provider "registry.terraform.io/hashicorp/google" {}\n'
        with open(self.canonical, "wb") as f:
            f.write(canonical_contents)

        result = workdir.seed_lock_file(self.workdir)

        self.assertTrue(result, "successful seed should return True")
        target = os.path.join(self.workdir, ".terraform.lock.hcl")
        self.assertTrue(os.path.isfile(target))
        with open(target, "rb") as f:
            self.assertEqual(f.read(), canonical_contents,
                             "seeded file must be byte-identical to canonical")

    # -- behaviour 3: clean fallback when canonical is absent --------------

    def test_missing_canonical_is_clean_fallback(self):
        """No canonical -> no-op, no exception.

        Matters for deployment shapes where provider_versions/ might not
        be bundled (early bootstrapping, custom Cloud Run image without
        the seed dir, etc.). The contract is "no canonical means
        terraform init resolves fresh from the registry" -- same as a
        clean checkout. seed_lock_file must NOT raise here.
        """
        # Sanity: no canonical present
        self.assertFalse(os.path.isfile(self.canonical))

        result = workdir.seed_lock_file(self.workdir)

        self.assertFalse(result, "no-canonical fallback returns False")
        target = os.path.join(self.workdir, ".terraform.lock.hcl")
        self.assertFalse(os.path.isfile(target),
                         "no file should have been created in the workdir")

    # -- canonical path resolves correctly ---------------------------------

    def test_canonical_path_resolves_under_repo_root(self):
        """canonical_lock_file_path() must point at provider_versions/ under repo root."""
        path = workdir.canonical_lock_file_path()
        expected = os.path.join(self.tmp, "provider_versions", ".terraform.lock.hcl")
        self.assertEqual(path, expected)


class SeedProvidersStubTests(unittest.TestCase):
    """Pin the seed_providers_stub() contract.

    Why this gets dedicated tests (D-6 fix, 2026-04-28):
    Without the providers stub seeded into a fresh workdir, the
    importer's preflight `terraform init` runs against an empty
    directory, creates `.terraform/` but downloads NO providers, and
    the KB-bootstrap that fires later sees an empty provider schema.
    The LLM then operates without grounding and hallucinates fields
    on complex resources (clusters, instances). The three behaviours
    pinned below mirror seed_lock_file's contract -- they prevent
    regressing the silent-no-provider-download failure mode.
    """

    def setUp(self):
        # Mirror SeedLockFileTests setup -- isolated tmp + patched
        # _repo_root so canonical_providers_seed_path() resolves
        # inside the sandbox.
        self._tmpdir = tempfile.TemporaryDirectory()
        self.tmp = self._tmpdir.name

        self.workdir = os.path.join(self.tmp, "imported", "test-proj-002")
        os.makedirs(self.workdir, exist_ok=True)

        self.canonical_dir = os.path.join(self.tmp, "provider_versions")
        os.makedirs(self.canonical_dir, exist_ok=True)
        self.canonical = os.path.join(self.canonical_dir,
                                      "_providers_seed.tf")

        self._patcher = patch.object(workdir, "_repo_root",
                                     return_value=self.tmp)
        self._patcher.start()

    def tearDown(self):
        self._patcher.stop()
        self._tmpdir.cleanup()

    # -- behaviour 1: never overwrite an existing seed ---------------------

    def test_existing_stub_is_never_overwritten(self):
        """If the workdir already has a `_providers_seed.tf`, the seed
        must NOT touch it. An operator that's pinned a custom provider
        version (e.g. for a beta feature test) must keep their pin --
        same logic as seed_lock_file's no-overwrite contract."""
        existing_contents = b'# operator-pinned providers; do not touch\n'
        target = os.path.join(self.workdir, "_providers_seed.tf")
        with open(target, "wb") as f:
            f.write(existing_contents)

        with open(self.canonical, "wb") as f:
            f.write(b"# canonical seed with DIFFERENT version\n")

        result = workdir.seed_providers_stub(self.workdir)

        self.assertFalse(result, "no-op should return False")
        with open(target, "rb") as f:
            self.assertEqual(f.read(), existing_contents,
                             "existing seed must be untouched")

    # -- behaviour 2: seed when stub is absent -----------------------------

    def test_absent_stub_gets_seeded_from_canonical(self):
        """Empty workdir + canonical present -> canonical is copied in.

        This is the D-6 happy path: a fresh per-project workdir gets
        the providers stub seeded so the upcoming `terraform init`
        actually downloads providers."""
        canonical_contents = (
            b'terraform {\n'
            b'  required_providers {\n'
            b'    google = {\n'
            b'      source  = "hashicorp/google"\n'
            b'      version = "7.29.0"\n'
            b'    }\n'
            b'  }\n'
            b'}\n'
        )
        with open(self.canonical, "wb") as f:
            f.write(canonical_contents)

        result = workdir.seed_providers_stub(self.workdir)

        self.assertTrue(result, "successful seed should return True")
        target = os.path.join(self.workdir, "_providers_seed.tf")
        self.assertTrue(os.path.isfile(target))
        with open(target, "rb") as f:
            self.assertEqual(f.read(), canonical_contents,
                             "seeded file must be byte-identical to canonical")

    # -- behaviour 3: clean fallback when canonical is absent --------------

    def test_missing_canonical_is_clean_fallback(self):
        """No canonical -> no-op, no exception. Mirrors seed_lock_file's
        deployment-shapes fallback (Cloud Run image without provider_versions/
        bundled, etc.). The contract is "no canonical means terraform init
        falls back to its default provider-resolution behaviour" -- which
        for D-6's purposes means the user will see the broken state again
        and need to debug it, but the seed function itself must not raise."""
        self.assertFalse(os.path.isfile(self.canonical))

        result = workdir.seed_providers_stub(self.workdir)

        self.assertFalse(result, "no-canonical fallback returns False")
        target = os.path.join(self.workdir, "_providers_seed.tf")
        self.assertFalse(os.path.isfile(target),
                         "no file should have been created in the workdir")

    # -- canonical path resolves correctly ---------------------------------

    def test_canonical_providers_seed_path_resolves_under_repo_root(self):
        """canonical_providers_seed_path() must point at
        provider_versions/_providers_seed.tf under repo root."""
        path = workdir.canonical_providers_seed_path()
        expected = os.path.join(self.tmp, "provider_versions",
                                "_providers_seed.tf")
        self.assertEqual(path, expected)


class ResolveBaseEnvVarTests(unittest.TestCase):
    """PUI-1B SMOKE 2026-04-28 regression: when MTAGENT_IMPORT_BASE
    is set (PSA-4 / Cloud Run mode), each call must re-read the env
    var. The OLD process-level cache returned the FIRST request's
    path forever, so per-request UUID rotation broke -- engine ran
    in stale workdir while PSA-4 hydrated/persisted in the new one.

    These tests pin the new caching policy:
      * env var SET   -> no cache; resolves fresh per call
      * env var UNSET -> cache (CLI / dev path; perf optimization)
    """

    def setUp(self):
        # Clear any cached state from a prior test in the same session.
        _workdir.reset_cache()

    def tearDown(self):
        _workdir.reset_cache()

    def test_env_var_change_between_calls_is_seen_immediately(self):
        """The bug: PSA-4 sets MTAGENT_IMPORT_BASE per request, expecting
        each request's resolve to see the new value. Pre-fix, the
        process cache returned the FIRST value forever.

        Uses tempfile-based absolute paths so the test works on both
        POSIX (/tmp/...) and Windows (C:\\Users\\...\\AppData\\Local\\Temp\\...)
        -- the resolved value differs per OS but the contract (NEW value
        returned on every call when env var changes) is universal.
        """
        with tempfile.TemporaryDirectory() as tmp_a, \
                tempfile.TemporaryDirectory() as tmp_b:
            with patch.dict(os.environ,
                            {"MTAGENT_IMPORT_BASE": tmp_a}):
                first = _workdir._resolve_base()
            self.assertEqual(first, tmp_a)

            # Second request, different UUID-scoped path.
            with patch.dict(os.environ,
                            {"MTAGENT_IMPORT_BASE": tmp_b}):
                second = _workdir._resolve_base()
            self.assertEqual(
                second, tmp_b,
                "env var change must propagate -- otherwise PSA-4's "
                "per-request UUID rotation breaks the engine workdir",
            )

    def test_env_var_unset_uses_cached_default(self):
        """CLI / dev path: no env var, the repo-relative default is
        cached for perf (resolve_project_workdir runs many times per
        engine call)."""
        env = {k: v for k, v in os.environ.items()
               if k != "MTAGENT_IMPORT_BASE"}
        with patch.dict(os.environ, env, clear=True):
            first = _workdir._resolve_base()
            second = _workdir._resolve_base()
        self.assertEqual(first, second)
        # Both should be a real path under repo root (./imported/)
        self.assertTrue(first.endswith("imported"),
                        f"unexpected default base: {first!r}")

    def test_env_var_relative_resolves_against_repo_root(self):
        """Relative env override resolves to repo-root/<override>."""
        with patch.dict(os.environ,
                        {"MTAGENT_IMPORT_BASE": "custom_imported"}):
            result = _workdir._resolve_base()
        self.assertTrue(result.endswith("custom_imported"))
        self.assertTrue(os.path.isabs(result))


if __name__ == "__main__":
    unittest.main()
