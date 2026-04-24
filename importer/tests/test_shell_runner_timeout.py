# importer/tests/test_shell_runner_timeout.py
"""Integration tests for the shell_runner subprocess timeout path.

These tests spawn REAL subprocesses (not mocks) because the failure
we're pinning -- ``process.communicate(timeout=...)`` raising
``TimeoutExpired`` and the wrapper translating it into ``UpstreamTimeout``
-- is specifically the behaviour of the real subprocess module. A
mock would test our own code against itself and miss the actual
integration.

We use ``sys.executable -c 'import time; time.sleep(N)'`` as the
slow subprocess because:
    - Portable: works on Windows, macOS, Linux without shelling to /bin/sleep.
    - Fast: a 2s timeout against a 10s sleep gives us a deterministic
      expiry without slowing the test suite down to minutes.
    - No external deps: doesn't require gcloud or terraform installed.

Why only two tests: the happy path (subprocess succeeds within the
timeout) is exercised by every other importer test that runs gcloud
or terraform. We only need to pin the NEW behaviour here (timeout
raises UpstreamTimeout with the right fields).
"""

from __future__ import annotations

import sys
import unittest

from common.errors import UpstreamTimeout
from importer import shell_runner


class ShellRunnerTimeoutTests(unittest.TestCase):

    def test_timeout_raises_upstream_timeout_with_fields(self):
        """When the subprocess runs past the timeout, we raise UpstreamTimeout.

        Pins:
            - exception type is UpstreamTimeout (not bare TimeoutExpired)
            - .binary reflects basename of the executable
            - .stage reflects the first command argument
            - .elapsed_s and .timeout_s are present for log emission
            - .__cause__ is the original subprocess.TimeoutExpired
              (operators can debug the original)
        """
        import subprocess  # local import: only needed in this test

        # sys.executable is the current python interpreter. The script
        # sleeps for 10s; our timeout is 1s. Expect UpstreamTimeout.
        cmd = [sys.executable, "-c", "import time; time.sleep(10)"]

        with self.assertRaises(UpstreamTimeout) as ctx:
            shell_runner.run_command(cmd, timeout=1.0)

        exc = ctx.exception
        # basename(sys.executable) is "python" or "python.exe"
        self.assertIn("python", exc.binary.lower())
        self.assertEqual(exc.stage, "-c",
                         "first arg becomes the stage for filtering")
        self.assertGreaterEqual(exc.elapsed_s, 0.5,
                                "elapsed must reflect actual wall-clock")
        self.assertLess(exc.elapsed_s, 10.0,
                        "elapsed must be less than the sleep duration "
                        "(proves we killed the child, didn't wait it out)")
        self.assertEqual(exc.timeout_s, 1.0)
        self.assertIsInstance(exc.__cause__, subprocess.TimeoutExpired,
                              "original TimeoutExpired preserved in __cause__")

    def test_timeout_env_override_is_respected(self):
        """MTAGENT_GCLOUD_TIMEOUT_S env var overrides the default.

        Matters for CI (slow registry) and staging (higher tolerance)
        -- operators must be able to tune without a redeploy.
        """
        import os

        cmd = [sys.executable, "-c", "import time; time.sleep(10)"]

        # With a tight env override, the default (60s) is NOT used.
        old = os.environ.get("MTAGENT_GCLOUD_TIMEOUT_S")
        os.environ["MTAGENT_GCLOUD_TIMEOUT_S"] = "1.0"
        try:
            with self.assertRaises(UpstreamTimeout) as ctx:
                # No explicit timeout -- should pick up env.
                shell_runner.run_command(cmd)
            self.assertEqual(ctx.exception.timeout_s, 1.0,
                             "env override must be read at call time")
        finally:
            if old is None:
                os.environ.pop("MTAGENT_GCLOUD_TIMEOUT_S", None)
            else:
                os.environ["MTAGENT_GCLOUD_TIMEOUT_S"] = old


if __name__ == "__main__":
    unittest.main()
