# my-terraform-agent/importer/shell_runner.py

import os
import subprocess
import shlex
import time

from common.logging import get_logger
from common.errors import UpstreamTimeout

log = get_logger(__name__)

# Default per-call timeout for any subprocess we wrap. Tuned for gcloud
# read calls (list / describe / search-all-resources): typical p50 ~1s,
# p99 ~10s, anything past 60s indicates the upstream is unresponsive,
# not just slow. Phase 0 audit (CC-2) flagged the absence of any
# timeout here as the single largest "request hangs forever on a flake"
# risk in the importer. Override per-call via the `timeout` param --
# never suppress entirely.
#
# Operators can tune via env (MTAGENT_GCLOUD_TIMEOUT_S) for testing or
# for environments with known-slow networking. The env override is
# read at call time, not import time, so tests can monkey-patch it
# without reloading the module.
_DEFAULT_TIMEOUT_S = 60.0


def _resolve_timeout(explicit: float | None) -> float:
    """Pick the active timeout: explicit arg > env override > module default."""
    if explicit is not None:
        return explicit
    env = os.environ.get("MTAGENT_GCLOUD_TIMEOUT_S")
    if env:
        try:
            return float(env)
        except ValueError:
            log.warning("gcloud_timeout_env_invalid",
                        value=env,
                        reason="MTAGENT_GCLOUD_TIMEOUT_S not a float; using default")
    return _DEFAULT_TIMEOUT_S


def run_command(command_args, *, timeout: float | None = None):
    """Execute a shell command and return stdout.

    Args:
        command_args: argv list. First element is the binary; logged as `cmd`.
        timeout: per-call wall-clock budget in seconds. Defaults to
            ``_DEFAULT_TIMEOUT_S`` (60s, suitable for gcloud read
            calls). Pass an explicit higher value for known-slow
            operations -- but never None or 0; uncapped subprocess
            calls are how you hang a Cloud Run request indefinitely.

    Raises:
        UpstreamTimeout: subprocess did not finish within the timeout.
            The original ``subprocess.TimeoutExpired`` is preserved
            in ``__cause__`` for debug.
        subprocess.CalledProcessError: subprocess exited non-zero.
            Existing behaviour, unchanged -- timeouts are the only
            new failure mode this function introduces.

    Implementation notes:
      * Uses ``subprocess.Popen`` (not ``subprocess.run``) so we can
        kill the child cleanly on timeout via ``process.kill()`` --
        ``run`` only sends SIGTERM, which gcloud has been observed
        to ignore for ~30s when stuck on the network.
      * Decodes bytes manually with ``errors='ignore'`` because some
        gcloud subcommands emit utf-8 with stray non-utf bytes from
        embedded JSON values (resource names with mojibake).
    """
    timeout_s = _resolve_timeout(timeout)

    # Event-style log: stable event name + structured args.
    # `cmd` is the shell-joined form (safe for quoting) so dashboards
    # can filter on a canonical binary name (e.g. cmd starts with "gcloud").
    log.info("subprocess_start",
             cmd=shlex.join(command_args),
             timeout_s=timeout_s)

    started = time.monotonic()
    # We use Popen for direct process control and PIPE to create the I/O streams.
    # We do NOT use text=True. We will handle bytes manually for maximum reliability.
    with subprocess.Popen(
        command_args,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE
    ) as process:
        try:
            # communicate(timeout=...) waits for the process and reads
            # both pipes; on timeout it raises subprocess.TimeoutExpired
            # and the child is left running -- we MUST kill it ourselves
            # in the except block to avoid leaking processes.
            stdout_bytes, stderr_bytes = process.communicate(timeout=timeout_s)
        except subprocess.TimeoutExpired as e:
            # Kill the runaway child cleanly; communicate() once more to
            # drain any remaining output (best-effort, swallow errors).
            process.kill()
            try:
                process.communicate(timeout=5)
            except Exception:
                pass
            elapsed = time.monotonic() - started
            binary = command_args[0] if command_args else "unknown"
            # Stage is just the binary's first subcommand (gcloud asset / terraform plan)
            # -- enough for log filtering without leaking project IDs.
            stage = command_args[1] if len(command_args) > 1 else "unknown"
            log.error("subprocess_timeout",
                      binary=os.path.basename(binary),
                      stage=stage,
                      elapsed_s=round(elapsed, 2),
                      timeout_s=timeout_s)
            raise UpstreamTimeout(
                f"{os.path.basename(binary)} {stage} timed out after {timeout_s:.0f}s "
                f"(elapsed {elapsed:.1f}s)",
                binary=os.path.basename(binary),
                stage=stage,
                elapsed_s=round(elapsed, 2),
                timeout_s=timeout_s,
                cmd=os.path.basename(binary),
            ) from e

        # Manually decode the byte streams into strings, ignoring potential decoding errors.
        stdout = stdout_bytes.decode('utf-8', errors='ignore')
        stderr = stderr_bytes.decode('utf-8', errors='ignore')

        # After the process is finished, we check its return code.
        if process.returncode != 0:
            log.error(
                "subprocess_failed",
                returncode=process.returncode,
                cmd=command_args[0] if command_args else "",
            )
            # We manually raise the exception.
            # Crucially, we populate the 'output' field with the combined, decoded streams.
            raise subprocess.CalledProcessError(
                returncode=process.returncode,
                cmd=command_args,
                output=stdout + stderr, # This is the combined output
                stderr=stderr
            )

    # If the process completed successfully, return the decoded stdout.
    return stdout
