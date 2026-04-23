# importer/knowledge_base.py
"""
Loads the per-resource Terraform provider schema document used to ground
LLM HCL generation.

History (why this module grew a bootstrap step)
-----------------------------------------------
The KB directory (`importer/knowledge_base/*.json`) is gitignored
(see `.gitignore`) because the files are auto-generated from the
provider schema and version-bound to whichever provider versions
`terraform init` has resolved locally. They're cheap to regenerate.

Before the bootstrap step, fresh checkouts hit a silent failure mode:
the KB file didn't exist, this loader returned `None`, the LLM
generated HCL without grounding, and we saw field-name hallucinations
(e.g. `consume_reservation_type` instead of `type` on
`reservation_affinity`). The user had to know to run `python build_kb.py`
manually — but nothing in the code surfaced that requirement.

The new flow on a miss:
    1. Attempt to bootstrap by calling the same schema-oracle path that
       `build_kb.py` uses (single source of truth — no duplication).
    2. If bootstrap succeeds, load the freshly written file and proceed.
    3. If bootstrap fails (e.g. `.terraform` not initialised, resource
       type not in the provider schema), print exactly what went wrong
       and fall back to no-context mode. The importer continues; the
       LLM just operates without grounding for that one resource.

Failure is fail-OPEN by design — a missing KB should never block the
importer from running. It just degrades quality. The user sees the
diagnostic and can decide whether to fix it.
"""

import json
import os
import sys

KB_DIR = os.path.join(os.path.dirname(__file__), 'knowledge_base')


def _attempt_bootstrap(resource_type: str) -> bool:
    """Generate the missing KB file from the live provider schema.

    Returns True iff the file now exists and parses. Any failure path
    is logged and returns False — callers fall back to no-context mode.

    Implementation note: we reuse `build_kb.build_one` / `write_one` so
    the on-disk shape is identical to what `python build_kb.py` would
    produce. No risk of two divergent generators producing subtly
    different schemas.
    """
    # Project root is a sibling of importer/. Make sure it's on sys.path
    # before importing build_kb, which lives at the root. Defensive: in
    # most invocation paths it's already there (running `python -m
    # importer.run` from root, or streamlit run from root).
    project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
    if project_root not in sys.path:
        sys.path.insert(0, project_root)

    try:
        # Lazy imports — keep this module cheap to load on the happy path
        # where the KB file exists. Importing build_kb pulls in
        # schema_oracle + the terraform-path resolver.
        import build_kb
        from importer import schema_oracle

        print(f"   - [BOOTSTRAP] KB miss for '{resource_type}'. Generating from `terraform providers schema -json`...")
        oracle = schema_oracle.get_oracle()
        doc = build_kb.build_one(resource_type, oracle)
        out_path = build_kb.write_one(resource_type, doc)
        args_n = len(doc.get("arguments", []))
        paths_n = len(doc.get("paths", {}))
        print(f"   - [BOOTSTRAP] OK: {out_path} ({args_n} args, {paths_n} paths)")
        return True

    except KeyError as e:
        # Resource type is not in the loaded provider schema — most
        # likely a typo in ASSET_TO_TERRAFORM_MAP, or a resource type
        # from a provider we don't have installed.
        print(f"   - [BOOTSTRAP] skipped: {e}")
        return False

    except RuntimeError as e:
        # schema_oracle raises this when `.terraform` isn't initialised
        # OR the terraform binary can't be located. Both have actionable
        # remediation in the error message itself.
        print(f"   - [BOOTSTRAP] failed: {e}")
        print("      Falling back to no-context mode for this resource.")
        return False

    except Exception as e:
        # Catch-all so a bootstrap bug never crashes the importer.
        # Importer continues in degraded mode; user sees the error.
        print(f"   - [BOOTSTRAP] unexpected error ({type(e).__name__}): {e}")
        print("      Falling back to no-context mode for this resource.")
        return False


def get_schema_for_resource(resource_type):
    """
    Loads the pre-generated documentation (schema) for a given resource
    type. On a cache miss, attempts to bootstrap the file from the live
    provider schema before giving up.

    Returns the parsed schema dict on success, or None if both load and
    bootstrap fail (importer is fail-open here — proceeds without
    documentation context).
    """
    file_path = os.path.join(KB_DIR, f"{resource_type}.json")
    print(f"[KB] Loading schema from {file_path}")

    # Bootstrap path: file missing -> try to generate it, then re-check.
    # We don't call _attempt_bootstrap unconditionally on every load
    # because the happy-path cost (a single os.path.exists) should stay
    # zero-network, zero-subprocess.
    if not os.path.exists(file_path):
        print("   - [KB] Schema file not found.")
        bootstrapped = _attempt_bootstrap(resource_type)
        if not bootstrapped or not os.path.exists(file_path):
            print("   - [KB] Proceeding without documentation context.")
            return None
        # Fall through to the normal load path with the freshly
        # written file.

    try:
        with open(file_path, "r", encoding="utf-8") as f:
            schema_data = json.load(f)
            print("   - [KB] Successfully loaded schema.")
            return schema_data
    except (IOError, json.JSONDecodeError) as e:
        print(f"   - [KB] Error reading schema file: {e}")
        return None
