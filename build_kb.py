# my-terraform-agent/build_kb.py
"""
Rebuilds `importer/knowledge_base/<tf_type>.json` from the schema oracle.

This replaces the prior markdown-scraping path (which hit raw.githubusercontent
and parsed `## Argument Reference` with a regex). Ground truth is now the
provider itself, via `terraform providers schema -json`, surfaced through
`importer.schema_oracle.get_oracle()`.

Output shape per file
---------------------
    {
      "resource_type":      "google_compute_instance",
      "provider":           "registry.terraform.io/hashicorp/google",
      "generated_from":     "terraform providers schema -json",
      "generator_version":  "schema_oracle/1",

      # Back-compat for importer/hcl_generator.py (reads `arguments[].name`).
      # Top-level only. Pure-computed and deprecated paths are filtered out.
      "arguments": [
        {"name": "boot_disk",       "required": true},
        {"name": "machine_type",    "required": true},
        ...
      ],

      # Full typed index — every dotted path in the resource schema.
      "paths": {
        "machine_type":                    {"kind":"attribute", "type":"string", "required":true},
        "boot_disk":                       {"kind":"block", "nesting_mode":"list", "min_items":1, "max_items":1, "required":true},
        "boot_disk.initialize_params":     {"kind":"block", ...},
        "boot_disk.initialize_params.image": {"kind":"attribute", "type":"string", "optional":true, "computed":true},
        ...
      },

      # Pre-computed rollups so downstream consumers (PR-3 auto-scrub,
      # drift detector rule-generation) don't have to walk `paths` each time.
      "required_paths":       [...],
      "computed_only_paths":  [...],
      "deprecated_paths":     [...]
    }

Usage
-----
    python build_kb.py                 # rebuild every resource in ASSET_TO_TERRAFORM_MAP
    python build_kb.py google_compute_instance google_storage_bucket
                                       # rebuild specific resources
    python build_kb.py --refresh       # also re-run `terraform providers schema -json`

The loader `importer/knowledge_base.py::get_schema_for_resource()` stays
untouched: the new shape is a strict superset of the old one.
"""

import json
import os
import sys
from typing import Dict, List

from importer import schema_oracle
from importer.config import ASSET_TO_TERRAFORM_MAP

KB_DIR = os.path.join(os.path.dirname(__file__), "importer", "knowledge_base")
GENERATOR_VERSION = "schema_oracle/1"
PROVIDER_ID = "registry.terraform.io/hashicorp/google"


def _is_top_level(path: str) -> bool:
    return "." not in path


def _serialize_attrinfo(info: schema_oracle.AttrInfo) -> dict:
    """Compact JSON view of an AttrInfo — only non-default fields are emitted
    to keep the file readable. Keys that are missing mean 'false' / 'unset'."""
    out: dict = {"kind": info.kind}
    if info.kind == "attribute":
        if info.type is not None:
            out["type"] = info.type
        for flag in ("required", "optional", "computed", "deprecated", "sensitive"):
            if getattr(info, flag):
                out[flag] = True
    else:  # block
        if info.nesting_mode:
            out["nesting_mode"] = info.nesting_mode
        if info.min_items is not None:
            out["min_items"] = info.min_items
        if info.max_items is not None:
            out["max_items"] = info.max_items
        for flag in ("required", "optional", "deprecated"):
            if getattr(info, flag):
                out[flag] = True
    return out


def _build_arguments_back_compat(tf_type: str,
                                 oracle: schema_oracle.SchemaOracle) -> List[dict]:
    """Produces the same-shape list the old markdown scraper produced, so
    importer/hcl_generator.py keeps working unchanged.

    Rules:
      * top-level paths only
      * drop deprecated paths (don't encourage new HCL to use them)
      * drop pure-computed attributes (users can't set them anyway)
      * include nested blocks (boot_disk, network_interface, ...) — the old
        scraper included them, and they ARE user-writable sections
    """
    args: List[dict] = []
    for path in oracle.list_paths(tf_type):
        if not _is_top_level(path):
            continue
        info = oracle.get(tf_type, path)
        if info is None or info.deprecated:
            continue
        if (info.kind == "attribute"
                and info.computed
                and not info.optional
                and not info.required):
            continue
        args.append({"name": path, "required": info.required})
    # Stable ordering: required first, then alpha — mirrors how a human reads docs.
    args.sort(key=lambda a: (not a["required"], a["name"]))
    return args


def build_one(tf_type: str, oracle: schema_oracle.SchemaOracle) -> dict:
    """Builds the KB document for a single resource type."""
    if not oracle.has(tf_type, provider=PROVIDER_ID):
        raise KeyError(
            f"Provider schema does not contain '{tf_type}'. "
            "The resource type may not exist, or the relevant provider may "
            "not be installed. Run `terraform init` and retry with --refresh."
        )

    paths_index: Dict[str, dict] = {}
    for path in oracle.list_paths(tf_type):
        info = oracle.get(tf_type, path)
        if info is not None:
            paths_index[path] = _serialize_attrinfo(info)

    return {
        "resource_type": tf_type,
        "provider": PROVIDER_ID,
        "generated_from": "terraform providers schema -json",
        "generator_version": GENERATOR_VERSION,
        "arguments": _build_arguments_back_compat(tf_type, oracle),
        "paths": paths_index,
        "required_paths": oracle.required_paths(tf_type),
        "computed_only_paths": oracle.computed_only_paths(tf_type),
        "deprecated_paths": oracle.deprecated_paths(tf_type),
    }


def write_one(tf_type: str, doc: dict) -> str:
    os.makedirs(KB_DIR, exist_ok=True)
    path = os.path.join(KB_DIR, f"{tf_type}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(doc, f, indent=2)
    return path


def build_all(tf_types: List[str], refresh: bool) -> int:
    oracle = schema_oracle.get_oracle(force_refresh=refresh)
    failed = 0
    for tf_type in tf_types:
        try:
            doc = build_one(tf_type, oracle)
            out_path = write_one(tf_type, doc)
            args_n = len(doc["arguments"])
            computed_n = len(doc["computed_only_paths"])
            paths_n = len(doc["paths"])
            print(f"  OK  {tf_type}: {args_n} top-level args, "
                  f"{computed_n} computed-only, {paths_n} total paths "
                  f"-> {out_path}")
        except Exception as e:
            print(f"  ERR {tf_type}: {e}")
            failed += 1
    return failed


def _parse_argv(argv: List[str]):
    args = argv[1:]
    refresh = "--refresh" in args
    explicit = [a for a in args if a != "--refresh"]
    if explicit:
        return explicit, refresh
    return sorted(set(ASSET_TO_TERRAFORM_MAP.values())), refresh


def main(argv: List[str]) -> int:
    tf_types, refresh = _parse_argv(argv)
    print(f"--- Rebuilding KB from schema oracle (refresh={refresh}) ---")
    print(f"    Targets: {len(tf_types)} resource type(s)")
    failed = build_all(tf_types, refresh=refresh)
    print(f"--- Done. {len(tf_types) - failed} succeeded, {failed} failed. ---")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
