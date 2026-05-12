"""Pre-push preflight for Migrator engine changes.

Codifies the post-mortem from Kiro v6/v7/v8 reviews into an automated
check. Run this AFTER every meaningful change to translator / sanitizer /
wiring / emitter code, BEFORE pushing.

What it does
============
1. **Fresh migration** on the customer fixture (or any path you point
   it at) so the emitted output is current.
2. **Tier 0/1** runs as normal — fast, always.
3. **Tier 2** on ONE canonical env (default: ``environments_dev``).
   Replaces the 6-min full sweep with a 30-second per-env check
   per the new process.
4. **Antipattern grep** across all emitted envs. Each known bug from
   Kiro v6+ has a literal pattern that should never appear in clean
   output. Non-zero count = regression.
5. **Documentation-accuracy** spot-check: the "Hardened defaults
   applied to" header lists services that ACTUALLY exist in this env.

Exit code 0 = clean. Non-zero = at least one antipattern found, see
output for which.

Run via:
    python scripts/preflight_migrator.py
    python scripts/preflight_migrator.py --skip-tier2
    python scripts/preflight_migrator.py --canonical-env environments_terarecon
    python scripts/preflight_migrator.py --repo /path/to/customer-fixtures

Discipline questions (NOT automated — human-only)
=================================================
Before declaring a fix done, ask yourself:
1. Did I grep the CUSTOMER SOURCE for all variants of the field I'm
   extracting? (Translator fields like cloudsql_instances vs sql_config
   vs database_instance bite when only one variant is supported.)
2. Did I grep MY EMITTED OUTPUT for the literal antipattern I just
   fixed, across ALL envs (not just the one I tested)?
3. Did I read 2 actual main.tf files end-to-end — one I expect to
   work AND one edge case (e.g., a satellite env with cross-env refs)?
4. Walking through `terraform apply` mentally: would each
   `default = "TODO-..."` value be accepted by the AWS API? If not,
   it should be `nullable = false` with no default.
5. Does each emitted documentation/header string match what's
   actually in the env? (HIPAA-header listing services-not-emitted
   is misleading.)
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import tempfile
from typing import Dict, List, Tuple


# ---------------------------------------------------------------------
# Known antipatterns. Each entry: (label, regex, severity, hint).
# Severity: "critical" = plan/apply failure; "minor" = cosmetic /
# functional degradation. Hint = where to look when one is found.
# ---------------------------------------------------------------------
_ANTIPATTERNS: List[Tuple[str, str, str, str]] = [
    # ---- Critical: would fail terraform plan or apply ----
    (
        "TODO-source-interpolation-needs-review (wholesale-replaced source value)",
        r"TODO-source-interpolation-needs-review",
        "critical",
        "rules_engine._clean_garbage_strings did a wholesale replacement "
        "instead of per-${} surgical replacement. The string ends up as a "
        "literal AWS API input value — invalid CIDR / DNS name / etc. — and "
        "terraform plan rejects it. See _replace_complex_interpolations in "
        "rules_engine.py. Kiro v9 #1+#2.",
    ),
    (
        "TODO-RESOLVE in emitted values",
        r"TODO-RESOLVE",
        "critical",
        "A translator's interpolation catch-all replaced ${...} with literal "
        "TODO-RESOLVE. Likely Aurora/RDS — let the customer-profile sanitizer "
        "handle interpolation; remove translator-level catch-all replace().",
    ),
    (
        'String-literal-interpolation antipattern (${"TODO-X"})',
        r'\$\{"TODO-',
        "critical",
        "Sanitizer wrapped a TODO marker in ${...}. Output should emit the "
        "bare TODO marker without the surrounding ${} so map keys + string "
        "values stay clean. See _sanitize_translation in terraform_emitter.",
    ),
    (
        "Aurora TODO_cluster_name map key",
        r'"TODO_cluster_name"\s*=',
        "critical",
        "Aurora translator fell through to placeholder. Customer source key "
        "is probably new (e.g., cloudsql_instances). Add to fallback list "
        "in aurora_postgres.py + rds.py.",
    ),
    (
        "Aurora TODO-cluster-name value",
        r'name\s*=\s*"TODO-cluster-name"',
        "critical",
        "Same as above — Aurora extraction failed.",
    ),
    (
        "Fragile positional VPC wiring (values()[0])",
        r"vpc_id\s+=\s+values\(module\.",
        "minor",
        "vpc_id should be a named-key lookup module.X.vpc_ids[\"specific_vpc\"] "
        "not values()[0]. Check provider_input_map='vpcs' on the wiring rule.",
    ),
    (
        "Fragile positional SNS wiring (values()[0]) on target_arn",
        r"target_arn\s+=\s+values\(module\.",
        "critical",
        "EventBridge schedule's target_arn fell back to values()[0]. The line "
        "should have an arn:aws:sns:... comment hint; if it does, the wiring's "
        "per-line resolver is broken. If no hint, the line shouldn't be "
        "wiring to SNS at all — check target_type.",
    ),
    (
        "Unwired vpc_id placeholder",
        r'vpc_id\s+=\s+"(?:TODO-vpc-id|vpc-TODO)"',
        "critical",
        "Wiring layer didn't substitute the VPC placeholder. Either no VPC "
        "module in env (add cross_env_var fallback) or the rule's "
        "todo_placeholder doesn't match the translator's actual string.",
    ),
    (
        "Broken instance_profile_name (TODO-dependency-ref-instance-profile)",
        r'"TODO-dependency-ref-instance-profile"',
        "critical",
        "EC2 translator built the profile name from a dependency-ref-style "
        "service_account_email. Skip the derivation when sa_email contains "
        "${...} or TODO-. See ec2.py.",
    ),
    (
        "Cross-env var default = TODO-supply-X (apply-time failure)",
        r'default\s+=\s+"TODO-supply-',
        "critical",
        "Cross-env variable still has a TODO-string default — AWS API will "
        "reject the literal at apply. Use `nullable = false` with NO default "
        "so terraform plan fails fast with a clear 'variable is required' message.",
    ),
    (
        "Unwired Firehose destination_bucket",
        r'destination_bucket\s+=\s+"TODO-firehose-destination-bucket"',
        "critical",
        "log-sink-firehose translator emitted the TODO placeholder but the "
        "wiring layer didn't substitute. Check the WiringRule for "
        "input_name=destination_bucket. Kiro v9 #3.",
    ),
    (
        "TODO-local-X / TODO-var-X in CIDR field (plan-breaks, operator-action)",
        r'cidr\s*=\s*"TODO-(local|var|each|dependency)-',
        # MINOR rather than CRITICAL because this is operator-action-required
        # output, not an engine regression: the source has unresolvable
        # ${local.X} interpolation and we surface it with a named TODO.
        # Preflight reports the count so a reviewer can compare to baseline;
        # if it grows after an engine change, that IS a regression.
        "minor",
        "AWS provider's client-side CIDR validation rejects the literal "
        "TODO string at plan time. Source has an unresolvable interpolation "
        "(e.g., local.secondary_subnet_cfgs.X.ip_cidr_range). The fix is "
        "operator-side: replace the TODO with the real CIDR. Engine-side: "
        "consider emitting a nullable=false `var.subnet_X_cidr` instead "
        "so plan fails with a clearer 'variable is required' message. "
        "Kiro v9 #1.",
    ),
    (
        "TODO-local-X in DNS field (plan-breaks, operator-action)",
        r'dns_name\s*=\s*"TODO-(local|var|each|dependency)-',
        "minor",
        "AWS provider rejects the literal TODO string as an invalid DNS "
        "name at plan time. Same root cause as cidr: source local can't "
        "be resolved. Operator must supply the real DNS name. Kiro v9 #2.",
    ),
    (
        "TODO-local-X in ARN field (plan-breaks)",
        r'(arn|certificate_arn|kms_key_arn|target_arn|role_arn)\s*=\s*"TODO-(local|var|each|dependency)-',
        "critical",
        "AWS provider validates ARN format client-side. TODO-local-X "
        "isn't a valid ARN. Either supply the real ARN via tfvars or use "
        "a cross-env var fallback (see cross_module_wiring.py).",
    ),
    (
        "TODO-local-X in IP address field (plan-breaks)",
        r'(ip_address|private_ip|public_ip|allocation_id)\s*=\s*"TODO-(local|var|each|dependency)-',
        "critical",
        "AWS provider validates IP format. TODO-local-X isn't a valid IP. "
        "Operator must replace.",
    ),
    (
        "Module emitted but missing from HIPAA header (compliance audit gap)",
        # Sentinel pattern — the actual check is done by
        # `_check_header_completeness`. Listed here so the antipatterns
        # report includes the bug class.
        r"__sentinel_never_matches__",
        "minor",
        "Header alias map doesn't cover this service. Add it to "
        "_HARDENED_TOKEN_TO_SERVICES in terraform_emitter.py.",
    ),
    (
        "Mangled interpolation that should have substituted",
        r"\$\{local_(?!environment\b|region\b|account_id\b)\w+\}",
        "minor",
        "python-hcl2 mangled `${local.foo}` to `${local_foo}`. Should have "
        "been caught by the customer profile loader's auto-alias generator. "
        "Add the dotted form to the customer profile YAML.",
    ),

    # ---- Empty translator outputs (translator didn't find source data) ----
    (
        "Log sinks empty (sinks = {})",
        r"^\s*sinks\s*=\s*\{\}",
        "minor",
        "log_sink translator returned no sinks. Customer source key may be "
        "unrecognised — add to fallback list in log_sink.py.",
    ),
    (
        "ACM certs empty (certificates = {})",
        r"^\s*certificates\s*=\s*\{\}",
        "minor",
        "ACM translator returned no certs. Check fallback source keys "
        "(certificates / classic_certificates / etc.) in acm.py.",
    ),
    (
        "Route53 zones empty (zones = {})",
        r"^\s*zones\s*=\s*\{\}",
        "minor",
        "Route53 rule returned no zones. Check for_each.source list and "
        "synthesize_when_empty fallback in the YAML rule.",
    ),
    (
        "ECR repos empty (repositories = {})",
        r"^\s*repositories\s*=\s*\{\}",
        "minor",
        "ECR rule returned no repos. Check for_each.source list in the "
        "YAML rule.",
    ),
    (
        "Aurora clusters empty (clusters = {})",
        r"^\s*clusters\s*=\s*\{\}",
        "critical",
        "Aurora translator returned no clusters. Check source-key fallback "
        "list (cloudsql_instances / sql_config / etc.) in aurora_postgres.py.",
    ),
]


def _emit_migration(repo_path: str, output_dir: str, skip_tier2: bool) -> Dict:
    """Run a fresh migration and return the MigrationResult.as_fields()."""
    # Lazy-import so this script can be linted without the engine being
    # importable in every CI shell.
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from migrator.run import run_migration
    from migrator.translate.customer_profile_loader import get_substitutions

    # Force-reload profile cache so dh.yaml edits take effect.
    get_substitutions.cache_clear()

    result = run_migration(
        repo_path,
        target_cloud="aws",
        target_format="terraform",
        output_dir=output_dir,
        project_id="preflight",
        skip_tier2=skip_tier2,
        compliance_profile="hipaa",
        customer_profile="dh",
    )
    return {
        "output_dir":  result.output_dir,
        "validation":  result.validation,
        "confidence":  result.confidence_summary,
    }


def _scan_antipatterns(envs_dir: str) -> Dict[str, List[Tuple[str, int]]]:
    """Walk every emitted env file and run each antipattern regex.
    Returns {label: [(env_name, count), ...]} for any non-zero matches.
    """
    findings: Dict[str, List[Tuple[str, int]]] = {}
    if not os.path.isdir(envs_dir):
        return findings

    # Per-env file content cache so each antipattern regex runs against
    # a pre-loaded string (avoids re-reading the same file 14 times).
    env_files: List[Tuple[str, str]] = []
    for env_name in sorted(os.listdir(envs_dir)):
        env_path = os.path.join(envs_dir, env_name)
        if not os.path.isdir(env_path):
            continue
        for fname in ("main.tf", "variables.tf"):
            full = os.path.join(env_path, fname)
            if os.path.isfile(full):
                try:
                    with open(full, "r", encoding="utf-8") as f:
                        env_files.append((env_name, f.read()))
                except (OSError, UnicodeDecodeError):
                    pass

    for label, pat, severity, _hint in _ANTIPATTERNS:
        compiled = re.compile(pat, re.MULTILINE)
        per_env: Dict[str, int] = {}
        for env_name, content in env_files:
            hits = len(compiled.findall(content))
            if hits:
                per_env[env_name] = per_env.get(env_name, 0) + hits
        if per_env:
            findings[label] = sorted(per_env.items(), key=lambda kv: -kv[1])
    return findings


def _check_header_accuracy(envs_dir: str) -> List[Tuple[str, str]]:
    """Bidirectional check on each env's HIPAA header:
    1. Header claims a service that isn't emitted (misleading — was the
       only direction the old version caught).
    2. A service IS emitted that SHOULD have been listed under the
       compliance profile but the header skipped it (compliance-audit
       gap — Kiro v9 #6).

    The token-to-service alias map mirrors the one in terraform_emitter.py.
    Keep them in sync; preflight regression catches mismatches.
    """
    issues: List[Tuple[str, str]] = []
    if not os.path.isdir(envs_dir):
        return issues

    # Tokens that compliance_profiles.py hardens — short slugs.
    # Map to the slug-set of translator service_names they cover.
    token_to_services = {
        "alb":     {"alb"},
        "eks":     {"eks-cluster"},
        "rds":     {"rds-postgres", "rds-mysql", "aurora-postgres"},
        "s3":      {"s3-bucket"},
        "secrets": {"secretsmanager-secret", "secrets-manager-secret"},
        "vpc":     {"vpc"},
    }

    header_re = re.compile(
        r"^# Hardened defaults applied to: (.+)$", re.MULTILINE,
    )
    profile_re = re.compile(
        r"^# Compliance profile: (\w+)$", re.MULTILINE,
    )
    # Match only LIVE source = "..." lines (not the scaffold-only
    # comment-prefixed ones the emitter writes for un-translated
    # resources like `#   source = "../../modules/eks-cluster"`).
    # Anchored to start-of-line (no leading `#`) via MULTILINE.
    source_re = re.compile(
        r'(?m)^[ \t]+source\s*=\s*"\.\./\.\./modules/([^/"]+)/?"',
    )

    for env_name in sorted(os.listdir(envs_dir)):
        main_tf = os.path.join(envs_dir, env_name, "main.tf")
        if not os.path.isfile(main_tf):
            continue
        try:
            with open(main_tf, "r", encoding="utf-8") as f:
                content = f.read()
        except (OSError, UnicodeDecodeError):
            continue

        profile_match = profile_re.search(content)
        if not profile_match or profile_match.group(1).lower() == "none":
            continue   # no compliance profile, no header to check

        m = header_re.search(content)
        if not m:
            continue

        listed = {s.strip() for s in m.group(1).split(",")}
        listed.discard("(none)")
        emitted_services = set(source_re.findall(content))

        # Direction 1: header claims X but X isn't emitted.
        for tok in sorted(listed):
            covers = token_to_services.get(tok, {tok})
            if not (covers & emitted_services):
                issues.append((
                    env_name,
                    f'header claims "{tok}" hardened, but no matching '
                    f'module ({", ".join(sorted(covers))}) is emitted here',
                ))

        # Direction 2: an emitted service is missing from the header.
        # Only relevant when the env DOES have a hardenable module but
        # the header doesn't list its token.
        for tok, covers in token_to_services.items():
            if tok in listed:
                continue
            matching_modules = covers & emitted_services
            if matching_modules:
                issues.append((
                    env_name,
                    f'module {next(iter(matching_modules))} is emitted with '
                    f'hardening defaults but header doesn\'t list token "{tok}" '
                    f'— compliance audit will miss it',
                ))
    return issues


def _print_findings(
    findings: Dict[str, List[Tuple[str, int]]],
    header_issues: List[Tuple[str, str]],
) -> bool:
    """Print a readable report. Returns True if any critical issue found."""
    critical_found = False

    if not findings and not header_issues:
        print("[OK] No antipatterns found in emitted output.")
        return False

    # Antipatterns
    if findings:
        print()
        print("=" * 70)
        print("ANTIPATTERN FINDINGS")
        print("=" * 70)
        for label, hits in findings.items():
            severity = next(s for lbl, _p, s, _h in _ANTIPATTERNS if lbl == label)
            hint = next(h for lbl, _p, _s, h in _ANTIPATTERNS if lbl == label)
            marker = "[!CRITICAL!]" if severity == "critical" else "[ minor ]"
            if severity == "critical":
                critical_found = True
            total = sum(c for _, c in hits)
            env_count = len(hits)
            print()
            print(f"{marker} {label}")
            print(f"   {total} occurrence(s) across {env_count} env(s)")
            for env, n in hits[:5]:
                print(f"     {n:>3}x  {env}")
            if len(hits) > 5:
                print(f"     ... and {len(hits) - 5} more env(s)")
            print(f"   hint: {hint}")

    # Header accuracy
    if header_issues:
        print()
        print("=" * 70)
        print("HEADER ACCURACY ISSUES (cosmetic but misleading)")
        print("=" * 70)
        for env, msg in header_issues:
            print(f"   {env}: {msg}")

    return critical_found


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--repo",
        default=r"C:\Users\41708\gcp-iac-fixtures\simple-gcp",
        help="Customer fixture repo path (default: simple-gcp customer fixture).",
    )
    parser.add_argument(
        "--canonical-env",
        default="environments_dev",
        help="Env name for the single-env Tier 2 check (default: environments_dev).",
    )
    parser.add_argument(
        "--skip-tier2",
        action="store_true",
        help="Skip the canonical-env terraform init+validate sweep.",
    )
    parser.add_argument(
        "--skip-migration",
        action="store_true",
        help="Use the existing migrator_output instead of running a fresh "
             "migration. Faster when you've just emitted and only want to "
             "re-check antipatterns.",
    )
    args = parser.parse_args()

    if not os.path.isdir(args.repo):
        print(f"[FAIL] Repo path not found: {args.repo}", file=sys.stderr)
        return 2

    output_dir = os.path.join(args.repo, "migrator_output")

    # Use a temp registry dir so the user's real ~/.migrator isn't touched.
    os.environ["MIGRATOR_REGISTRY_DIR"] = tempfile.mkdtemp(
        prefix="preflight_registry_",
    )

    # --- 1. Migration ---
    if args.skip_migration:
        if not os.path.isdir(output_dir):
            print("[FAIL] --skip-migration requested but no migrator_output "
                  "exists. Drop the flag to emit fresh.", file=sys.stderr)
            return 2
        print(f"[skip] Using existing migrator_output at {output_dir}")
        # Best-effort validation read from the engine's last persisted state.
        validation_summary = None
    else:
        print(f"[run]  Emitting migration on {args.repo} ...")
        try:
            engine_out = _emit_migration(
                args.repo, output_dir, skip_tier2=True,
            )
        except Exception as e:  # noqa: BLE001
            print(f"[FAIL] Migration crashed: {type(e).__name__}: {e}",
                  file=sys.stderr)
            return 2
        validation_summary = engine_out["validation"]
        print(f"[run]  Resources: {engine_out['confidence']}")
        # Engine-reported tier failures are CRITICAL — they mean the
        # emitted output won't parse or won't fmt-check. Always block
        # the push when these are non-zero. (Previously preflight only
        # warned and exited 0 if no antipatterns matched — caused
        # me to miss a sb_demo HCL-parse failure I'd introduced.)
        engine_tier_failures = False
        if validation_summary and not validation_summary.get("overall_passed"):
            for t in validation_summary.get("tiers", []):
                if t.get("status") == "failed":
                    engine_tier_failures = True
                    print(f"[FAIL] Engine tier{t['tier']}: {t['failure_count']} failures")
                    for f in t.get("failures", [])[:3]:
                        print(f"       {str(f)[:200]}")

    # --- 2. Antipattern grep ---
    envs_dir = os.path.join(output_dir, "target", "environments")
    findings = _scan_antipatterns(envs_dir)
    header_issues = _check_header_accuracy(envs_dir)

    # --- 3. Canonical-env Tier 2 ---
    tier2_pass = True
    if not args.skip_tier2:
        env_path = os.path.join(envs_dir, args.canonical_env)
        if not os.path.isdir(env_path):
            print(f"[WARN] Canonical env {args.canonical_env} not found in output. "
                  "Skipping Tier 2.")
        else:
            print(f"[run]  Tier 2 on {args.canonical_env} ...")
            tier2_pass = _run_canonical_tier2(env_path)
            if tier2_pass:
                print(f"[OK]   Tier 2 clean on {args.canonical_env}")
            else:
                print(f"[FAIL] Tier 2 failed on {args.canonical_env} (see output above)")

    # --- 4. Report + exit code ---
    critical = _print_findings(findings, header_issues)

    # Pull `engine_tier_failures` from the migration path if it ran.
    # When --skip-migration was used, default to False (existing output
    # is presumed clean per the prior emit run).
    if not args.skip_migration:
        # captured into closure-style local — re-derive from validation
        if validation_summary and not validation_summary.get("overall_passed"):
            engine_failed = any(
                t.get("status") == "failed"
                for t in validation_summary.get("tiers", [])
            )
        else:
            engine_failed = False
    else:
        engine_failed = False

    print()
    print("=" * 70)
    if critical or not tier2_pass or engine_failed:
        print("PREFLIGHT FAILED — fix above issues before pushing.")
        print("=" * 70)
        return 1
    if findings or header_issues:
        print("Preflight passed with MINOR issues — review above; ok to push.")
        print("=" * 70)
        return 0
    print("PREFLIGHT CLEAN.")
    print("=" * 70)
    return 0


def _run_canonical_tier2(env_path: str) -> bool:
    """Run terraform init + validate on a single env. Returns True on
    success; prints terraform's own output to stderr/stdout on failure."""
    import subprocess
    import shutil

    # Wipe any prior .terraform so init is clean
    tf_dir = os.path.join(env_path, ".terraform")
    if os.path.isdir(tf_dir):
        shutil.rmtree(tf_dir, ignore_errors=True)
    lock = os.path.join(env_path, ".terraform.lock.hcl")
    if os.path.isfile(lock):
        try:
            os.remove(lock)
        except OSError:
            pass

    try:
        subprocess.run(
            ["terraform", "init", "-backend=false", "-input=false"],
            cwd=env_path, check=True,
            capture_output=True, timeout=120,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
        print(f"[FAIL] terraform init: {e}", file=sys.stderr)
        if hasattr(e, "stderr") and e.stderr:
            print(e.stderr.decode("utf-8", errors="replace")[:2000],
                  file=sys.stderr)
        return False

    try:
        subprocess.run(
            ["terraform", "validate"],
            cwd=env_path, check=True,
            capture_output=True, timeout=60,
        )
    except subprocess.CalledProcessError as e:
        print("[FAIL] terraform validate failures:", file=sys.stderr)
        if e.stdout:
            print(e.stdout.decode("utf-8", errors="replace")[:3000])
        if e.stderr:
            print(e.stderr.decode("utf-8", errors="replace")[:1500],
                  file=sys.stderr)
        return False
    except subprocess.TimeoutExpired:
        print("[FAIL] terraform validate timed out", file=sys.stderr)
        return False

    return True


if __name__ == "__main__":
    sys.exit(main())
