# policy/engine.py
"""
OPA / Conftest evaluation engine.

Runs Rego policies (under `package main`) against a JSON document and returns
structured `Violation` records. Conftest is invoked as a subprocess with
`--output json --no-color` and the document piped in via stdin.

Policy file convention
----------------------
Each .rego file lives under `policy/policies/{common|<tf_type>}/<rule_id>.rego`,
declares `package main`, and emits violations through `deny`. The deny
message MUST be a sprintf'd string with the prefix `[SEVERITY][rule_id]`:

    package main

    deny[msg] {
        not input.versioning.enabled
        msg := sprintf(
            "[HIGH][bucket_versioning] versioning must be enabled on bucket %s",
            [input.name],
        )
    }

Why prefix-encoding instead of Rego metadata annotations? Conftest's
annotation pipeline requires extra flags and only some output modes carry
them through. The prefix is portable to raw `opa eval`, trivial to parse
in Python, and shows up readable even when conftest is run by hand.

Conftest must be on PATH. We fail fast with install instructions.

Inputs the policies see
-----------------------
The engine is called with the raw `gcloud ... describe` JSON for the
resource (camelCase, GCP API field names). This is intentional: compliance
teams know the cloud field names, and policies stay portable to other
contexts (terraform-show-json plan output uses snake_case and would need a
separate set, but that's a step-2 concern — see module docstring).
"""

import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from . import config as _policy_config


# Match `[SEVERITY][rule_id] message text`. Severity is uppercase and one
# of HIGH/MED/LOW; rule_id is any non-bracket chars.
_VIOLATION_RE = re.compile(r"^\[(HIGH|MED|LOW)\]\[([^\]]+)\]\s*(.*)$")


@dataclass
class Violation:
    severity: str           # "HIGH" | "MED" | "LOW"
    rule_id: str            # short slug, e.g. "bucket_versioning"
    message: str            # human-readable (post prefix-strip)
    resource_address: str   # tf address, e.g. "google_storage_bucket.foo"
    policy_file: str        # resolved path to the .rego that fired

    @property
    def severity_weight(self) -> int:
        # Imported here to avoid a circular import at module load time.
        from . import config
        return config.SEVERITY_WEIGHTS.get(self.severity, 0)


_CONFTEST_MISSING_HINT = (
    "Conftest is required but not found on PATH.\n"
    "  Install:\n"
    "    Windows : choco install conftest\n"
    "              (or download a release: https://github.com/open-policy-agent/conftest/releases)\n"
    "    macOS   : brew install conftest\n"
    "    Linux   : see https://www.conftest.dev/install/\n"
    "  Verify  : conftest --version"
)


def ensure_conftest_available() -> None:
    """Raise RuntimeError with install instructions if conftest is missing.

    Callers that need to fail fast (the standalone CLI) call this up front.
    Callers that should fail-open (the detector decoration) catch the error
    and skip policy evaluation rather than crash a critical path.
    """
    if shutil.which("conftest") is None:
        raise RuntimeError(_CONFTEST_MISSING_HINT)


def evaluate(
    document: Dict[str, Any],
    policy_dirs: List[str],
    resource_address: str,
) -> List[Violation]:
    """Run conftest against `document` using policies from `policy_dirs`.

    Parameters
    ----------
    document
        Parsed JSON object — typically the cloud snapshot for one resource.
    policy_dirs
        List of directories containing .rego files. All directories are
        passed as additive `--policy` flags. Dirs that don't exist or
        contain no .rego files are silently skipped (lets callers add
        new resource types without first creating empty dirs).
    resource_address
        TF address used to tag returned violations.

    Returns
    -------
    List of Violation. Empty list when policies pass cleanly OR when the
    engine errors mid-evaluation (we surface a one-line warning in that
    case instead of crashing — the caller decides whether to fail).
    """
    ensure_conftest_available()

    real_dirs = []
    for d in policy_dirs:
        if not os.path.isdir(d):
            continue
        if not any(f.endswith(".rego") for f in os.listdir(d)):
            continue
        real_dirs.append(d)
    if not real_dirs:
        return []

    cmd = ["conftest", "test", "--output", "json", "--no-color"]
    for d in real_dirs:
        cmd.extend(["--policy", d])
    cmd.append("-")  # read input document from stdin

    try:
        proc = subprocess.run(
            cmd,
            input=json.dumps(document),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
        )
    except subprocess.TimeoutExpired:
        print(f"   ⚠️  Conftest timed out evaluating {resource_address} (>30s)")
        return []
    except (OSError, subprocess.SubprocessError) as e:
        print(f"   ⚠️  Conftest invocation failed for {resource_address}: {e}")
        return []

    # Conftest exits 0 (no failures), 1 (failures present), 2+ (engine error).
    if proc.returncode not in (0, 1):
        first = (proc.stderr or "").strip().splitlines()
        print(f"   ⚠️  Conftest engine error evaluating {resource_address}: "
              f"{first[0] if first else 'unknown'}")
        return []

    try:
        results = json.loads(proc.stdout) if proc.stdout.strip() else []
    except json.JSONDecodeError:
        return []

    violations: List[Violation] = []
    for file_result in results:
        for failure in file_result.get("failures") or []:
            v = _parse_violation(
                raw_msg=failure.get("msg") or "",
                addr=resource_address,
                policy_dirs=real_dirs,
            )
            if v is not None:
                violations.append(v)
        # Rego `warn` rules surface here; treat as MED if not otherwise tagged.
        for warning in file_result.get("warnings") or []:
            v = _parse_violation(
                raw_msg=warning.get("msg") or "",
                addr=resource_address,
                policy_dirs=real_dirs,
            )
            if v is not None:
                violations.append(v)

    # P4-1 per-call cap: if a single resource produces an unreasonable
    # number of violations (buggy rule iterating a long list, malicious
    # .tf crafted to detonate one), truncate at the cap and emit a
    # one-line warning. Prevents the worst case where a single API call
    # returns thousands of violation records.
    cap = _policy_config.MAX_VIOLATIONS_PER_CALL
    if len(violations) > cap:
        truncated = len(violations) - cap
        print(f"   ⚠️  Truncated {truncated} additional violations on "
              f"{resource_address} (cap: {cap}/call). "
              f"This usually indicates a buggy rule iterating a long "
              f"list -- please review.")
        violations = violations[:cap]

    return violations


def _parse_violation(raw_msg: str, addr: str,
                     policy_dirs: List[str]) -> Optional[Violation]:
    """Extract severity / rule_id / message from `[SEVERITY][rule_id] text`."""
    raw = raw_msg.strip()
    if not raw:
        return None
    m = _VIOLATION_RE.match(raw)
    if not m:
        # Unparseable — surface anyway at LOW so the user notices and can
        # fix the policy author's missing prefix.
        return Violation(
            severity="LOW",
            rule_id="unparsed",
            message=raw,
            resource_address=addr,
            policy_file="(unknown)",
        )
    severity, rule_id, message = m.group(1), m.group(2), m.group(3)
    return Violation(
        severity=severity,
        rule_id=rule_id,
        message=message,
        resource_address=addr,
        policy_file=_resolve_policy_file(rule_id, policy_dirs),
    )


def _resolve_policy_file(rule_id: str, policy_dirs: List[str]) -> str:
    """Find the .rego file that owns this rule_id.

    By convention rule_id matches the filename (stem). Search every dir
    we evaluated against; first hit wins. Returns "(unknown)" if not
    found (which would mean the convention was broken — the policy still
    fired, we just can't show its source path in the report).
    """
    candidate = f"{rule_id}.rego"
    for d in policy_dirs:
        path = os.path.join(d, candidate)
        if os.path.isfile(path):
            return path
    return "(unknown)"
