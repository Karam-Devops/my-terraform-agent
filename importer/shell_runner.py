# importer/shell_runner.py
import subprocess
import shlex

def run_command(command_args, capture=True):
    print(f"\n▶️  Executing: {shlex.join(command_args)}")
    try:
        process = subprocess.run(
            command_args, check=True, capture_output=capture, text=True, encoding='utf-8'
        )
        return process.stdout
    except subprocess.CalledProcessError as e:
        print(f"❌ Error executing command: {shlex.join(command_args)}")
        if capture: print(f"   Stderr: {e.stderr}")
        return None
    except FileNotFoundError:
        print(f"❌ Error: The executable '{command_args[0]}' was not found.")
        print("   Please ensure path variables in importer/config.py are correct.")
        return None