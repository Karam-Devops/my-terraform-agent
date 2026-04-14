# my-terraform-agent/importer/shell_runner.py

import subprocess
import shlex

def run_command(command_args):
    """
    Executes a shell command using the low-level Popen interface to guarantee
    capture of stdout/stderr by reading raw byte streams.
    """
    print(f"\n▶️  Executing: {shlex.join(command_args)}")

    # We use Popen for direct process control and PIPE to create the I/O streams.
    # We do NOT use text=True. We will handle bytes manually for maximum reliability.
    with subprocess.Popen(
        command_args,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE
    ) as process:
        # The .communicate() method waits for the process to finish and
        # reliably reads ALL data from both pipes as raw bytes.
        stdout_bytes, stderr_bytes = process.communicate()

        # Manually decode the byte streams into strings, ignoring potential decoding errors.
        stdout = stdout_bytes.decode('utf-8', errors='ignore')
        stderr = stderr_bytes.decode('utf-8', errors='ignore')

        # After the process is finished, we check its return code.
        if process.returncode != 0:
            print(f"❌ Command failed. Raising exception with captured output.")
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