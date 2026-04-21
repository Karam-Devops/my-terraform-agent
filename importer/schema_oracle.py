# importer/schema_oracle.py
"""
Schema Authority Layer.

Loads the Terraform provider schema (via `terraform providers schema -json`)
and exposes typed, dotted-path lookups for any attribute or nested block.

This is the authoritative replacement for the markdown-scraping path in
build_kb.py. The provider schema is what Terraform itself uses to validate
HCL; reading it directly removes an entire category of LLM mistakes (writing
computed-only fields, mis-shaping required blocks, missing the `deprecated`
flag).

Generation is on-demand and cached on disk at `.terraform_schema_cache.json`
in the project root. Subsequent calls in the same Python process are served
from an in-memory index.

Path syntax: dot-separated, no list indices.
  Examples
    "machine_type"
    "boot_disk.initialize_params.image"
    "service_account.scopes"

This file is intentionally additive. Nothing in the existing importer or
detector is rewired yet — that happens in PR-2 onward.
"""

import json
import os
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Cache location. Lives at project root so it sits next to .terraform/.
# Recommend gitignoring (see .gitignore update in this PR).
_SCHEMA_CACHE_FILENAME = ".terraform_schema_cache.json"

# Default GCP provider in the registry. Override via `provider=` on each call
# if/when AWS / Azure schemas need to be queried.
_DEFAULT_PROVIDER = "registry.terraform.io/hashicorp/google"


def _project_root() -> str:
    return os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))


def _schema_cache_path() -> str:
    return os.path.join(_project_root(), _SCHEMA_CACHE_FILENAME)


# ---------------------------------------------------------------------------
# Public data model
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class AttrInfo:
    """Type-safe view of a schema attribute or nested block.

    For attributes: `kind == "attribute"`, `type` is the raw schema type
    (e.g. "string", ["map","string"], ["list",["object",{...}]]).

    For nested blocks: `kind == "block"`, `nesting_mode` is "list" / "set" /
    "single" / "map", and `min_items` / `max_items` may be set.
    """

    path: str
    kind: str  # "attribute" | "block"
    type: Optional[Any] = None
    required: bool = False
    optional: bool = False
    computed: bool = False
    deprecated: bool = False
    sensitive: bool = False
    description: Optional[str] = None
    nesting_mode: Optional[str] = None
    min_items: Optional[int] = None
    max_items: Optional[int] = None


# ---------------------------------------------------------------------------
# Schema generation + load
# ---------------------------------------------------------------------------

def _generate_schema(dest_path: str) -> None:
    """Runs `terraform providers schema -json` and writes the dump to dest.

    Uses tempfile + atomic os.replace so a partial dump on failure never
    poisons the cache. Streams stdout to disk in binary mode to dodge
    Windows console-encoding pitfalls.
    """
    root = _project_root()
    if not os.path.isdir(os.path.join(root, ".terraform")):
        raise RuntimeError(
            "Terraform is not initialised in the project root. "
            "Run `terraform init` (or `terraform init -upgrade`) first so the "
            "provider plugins are available locally; the schema dump cannot "
            "be produced without them."
        )

    fd, tmp_path = tempfile.mkstemp(suffix=".json", prefix=".tf_schema_")
    os.close(fd)
    try:
        with open(tmp_path, "wb") as out:
            subprocess.run(
                ["terraform", "providers", "schema", "-json"],
                cwd=root,
                stdout=out,
                stderr=subprocess.PIPE,
                check=True,
            )
        # Validate the dump parses before promoting it to the cache slot.
        with open(tmp_path, "r", encoding="utf-8") as f:
            json.load(f)
        os.replace(tmp_path, dest_path)
    except subprocess.CalledProcessError as e:
        stderr = (e.stderr or b"").decode("utf-8", errors="replace")
        raise RuntimeError(
            f"`terraform providers schema -json` failed (exit {e.returncode}):\n{stderr}"
        ) from e
    finally:
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass


def _load_raw(force_refresh: bool = False) -> dict:
    cache = _schema_cache_path()
    if force_refresh or not os.path.isfile(cache):
        _generate_schema(cache)
    with open(cache, "r", encoding="utf-8") as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# Indexed oracle
# ---------------------------------------------------------------------------

class SchemaOracle:
    """Lazily-built, in-memory path index over one or more provider schemas.

    Usage
    -----
        oracle = get_oracle()
        oracle.is_computed("google_compute_instance", "terraform_labels")  # True
        oracle.is_required("google_compute_instance", "machine_type")       # True
        oracle.computed_only_paths("google_compute_instance")
            # -> ['cpu_platform', 'creation_timestamp', 'effective_labels', ...]
    """

    def __init__(self, raw: dict):
        self._raw = raw
        # provider_id -> { tf_type -> { dotted_path -> AttrInfo } }
        self._index: Dict[str, Dict[str, Dict[str, AttrInfo]]] = {}

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------

    @classmethod
    def load(cls, force_refresh: bool = False) -> "SchemaOracle":
        return cls(_load_raw(force_refresh=force_refresh))

    # ------------------------------------------------------------------
    # Internal walker
    # ------------------------------------------------------------------

    def _build_index_for(self, provider_id: str, tf_type: str) -> Dict[str, AttrInfo]:
        prov = self._raw.get("provider_schemas", {}).get(provider_id)
        if prov is None:
            raise KeyError(f"Provider not present in schema dump: {provider_id}")
        rsrc = (prov.get("resource_schemas") or {}).get(tf_type)
        if rsrc is None:
            raise KeyError(
                f"Resource type not present in provider {provider_id}: {tf_type}"
            )
        flat: Dict[str, AttrInfo] = {}
        self._walk_block(rsrc.get("block", {}), prefix="", out=flat)
        return flat

    def _walk_block(self, block: dict, prefix: str, out: Dict[str, AttrInfo]) -> None:
        for name, spec in (block.get("attributes") or {}).items():
            path = f"{prefix}.{name}" if prefix else name
            out[path] = AttrInfo(
                path=path,
                kind="attribute",
                type=spec.get("type"),
                required=bool(spec.get("required")),
                optional=bool(spec.get("optional")),
                computed=bool(spec.get("computed")),
                deprecated=bool(spec.get("deprecated")),
                sensitive=bool(spec.get("sensitive")),
                description=spec.get("description"),
            )
        for name, spec in (block.get("block_types") or {}).items():
            path = f"{prefix}.{name}" if prefix else name
            # A nested block is *required* in HCL iff min_items >= 1. Terraform
            # does not expose a raw `required` flag on blocks — it must be
            # derived from the cardinality constraints.
            min_items = spec.get("min_items")
            required_block = bool(min_items and min_items >= 1)
            out[path] = AttrInfo(
                path=path,
                kind="block",
                required=required_block,
                optional=not required_block,
                nesting_mode=spec.get("nesting_mode"),
                min_items=min_items,
                max_items=spec.get("max_items"),
            )
            self._walk_block(spec.get("block") or {}, prefix=path, out=out)

    def _ensure(self, tf_type: str, provider: str) -> Dict[str, AttrInfo]:
        prov_idx = self._index.setdefault(provider, {})
        if tf_type not in prov_idx:
            prov_idx[tf_type] = self._build_index_for(provider, tf_type)
        return prov_idx[tf_type]

    # ------------------------------------------------------------------
    # Public accessors — single-path queries
    # ------------------------------------------------------------------

    def has(self, tf_type: str, provider: str = _DEFAULT_PROVIDER) -> bool:
        prov = self._raw.get("provider_schemas", {}).get(provider, {})
        return tf_type in (prov.get("resource_schemas") or {})

    def get(self, tf_type: str, path: str,
            provider: str = _DEFAULT_PROVIDER) -> Optional[AttrInfo]:
        return self._ensure(tf_type, provider).get(path)

    def is_computed(self, tf_type: str, path: str,
                    provider: str = _DEFAULT_PROVIDER) -> bool:
        info = self.get(tf_type, path, provider)
        return bool(info and info.computed)

    def is_required(self, tf_type: str, path: str,
                    provider: str = _DEFAULT_PROVIDER) -> bool:
        info = self.get(tf_type, path, provider)
        return bool(info and info.required)

    def is_optional(self, tf_type: str, path: str,
                    provider: str = _DEFAULT_PROVIDER) -> bool:
        info = self.get(tf_type, path, provider)
        return bool(info and info.optional)

    def is_deprecated(self, tf_type: str, path: str,
                      provider: str = _DEFAULT_PROVIDER) -> bool:
        info = self.get(tf_type, path, provider)
        return bool(info and info.deprecated)

    def is_computed_only(self, tf_type: str, path: str,
                         provider: str = _DEFAULT_PROVIDER) -> bool:
        """True iff the field is `computed` AND neither `required` nor
        `optional` — i.e. read-only. These must NEVER be written by the LLM."""
        info = self.get(tf_type, path, provider)
        return bool(info and info.computed and not info.optional and not info.required)

    def type_of(self, tf_type: str, path: str,
                provider: str = _DEFAULT_PROVIDER) -> Optional[Any]:
        info = self.get(tf_type, path, provider)
        return info.type if info else None

    # ------------------------------------------------------------------
    # Public accessors — bulk queries
    # ------------------------------------------------------------------

    def list_paths(self, tf_type: str, kind: Optional[str] = None,
                   provider: str = _DEFAULT_PROVIDER) -> List[str]:
        """All paths in the resource. `kind` filters to 'attribute' or 'block'."""
        idx = self._ensure(tf_type, provider)
        return sorted(p for p, info in idx.items() if kind is None or info.kind == kind)

    def writable_paths(self, tf_type: str,
                       provider: str = _DEFAULT_PROVIDER,
                       kinds: tuple = ("attribute", "block")) -> List[str]:
        """Paths the user may set in HCL — required or optional, not
        deprecated. Includes both attributes *and* nested blocks by default
        (a user writes `boot_disk { ... }` as much as `machine_type = ...`).
        Pass `kinds=("attribute",)` to restrict to scalars/maps only."""
        idx = self._ensure(tf_type, provider)
        return sorted(
            p for p, info in idx.items()
            if info.kind in kinds
            and (info.required or info.optional)
            and not info.deprecated
        )

    def required_paths(self, tf_type: str,
                       provider: str = _DEFAULT_PROVIDER) -> List[str]:
        idx = self._ensure(tf_type, provider)
        return sorted(p for p, info in idx.items() if info.required)

    def computed_only_paths(self, tf_type: str,
                            provider: str = _DEFAULT_PROVIDER) -> List[str]:
        """Paths to silently strip from a cloud snapshot before the LLM sees
        it. These are the fields that — pre-PR-1 — caused the LLM to emit
        invalid HCL and that `heuristics.json` was patching by hand."""
        idx = self._ensure(tf_type, provider)
        return sorted(
            p for p, info in idx.items()
            if info.kind == "attribute"
            and info.computed and not info.optional and not info.required
        )

    def deprecated_paths(self, tf_type: str,
                         provider: str = _DEFAULT_PROVIDER) -> List[str]:
        idx = self._ensure(tf_type, provider)
        return sorted(p for p, info in idx.items() if info.deprecated)


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_SINGLETON: Optional[SchemaOracle] = None


def get_oracle(force_refresh: bool = False) -> SchemaOracle:
    """Returns the process-wide oracle, building it on first call."""
    global _SINGLETON
    if _SINGLETON is None or force_refresh:
        _SINGLETON = SchemaOracle.load(force_refresh=force_refresh)
    return _SINGLETON


# ---------------------------------------------------------------------------
# CLI smoke-test
#   python -m importer.schema_oracle google_compute_instance
#   python -m importer.schema_oracle google_compute_instance --refresh
# ---------------------------------------------------------------------------

def _cli(argv: List[str]) -> int:
    if len(argv) < 2:
        print("Usage: python -m importer.schema_oracle <tf_type> [--refresh]")
        return 2
    tf_type = argv[1]
    refresh = "--refresh" in argv[2:]

    print(f"--- Loading oracle (refresh={refresh}) ---")
    oracle = get_oracle(force_refresh=refresh)
    if not oracle.has(tf_type):
        print(f"Resource type not found in provider schema: {tf_type}")
        return 1

    writable = oracle.writable_paths(tf_type)
    required = oracle.required_paths(tf_type)
    computed_only = oracle.computed_only_paths(tf_type)
    deprecated = oracle.deprecated_paths(tf_type)
    blocks = oracle.list_paths(tf_type, kind="block")

    print(f"\n=== {tf_type} ===")
    print(f"Writable attribute paths : {len(writable)}")
    print(f"Required paths           : {len(required)}")
    print(f"Computed-only paths      : {len(computed_only)}")
    print(f"Deprecated paths         : {len(deprecated)}")
    print(f"Nested blocks            : {len(blocks)}")

    print("\nRequired:")
    for p in required:
        print(f"  * {p}")

    print("\nComputed-only (auto-strip from cloud snapshot before LLM):")
    for p in computed_only:
        info = oracle.get(tf_type, p)
        print(f"  - {p}  (type={info.type})")

    if deprecated:
        print("\nDeprecated (skip when generating new HCL):")
        for p in deprecated:
            print(f"  ! {p}")

    return 0


if __name__ == "__main__":
    sys.exit(_cli(sys.argv))
