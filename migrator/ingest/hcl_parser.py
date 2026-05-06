"""Parse Terraform / Terragrunt HCL using python-hcl2.

Wraps the third-party hcl2 module behind a tiny stable interface so we
can swap parsers later without touching callers. Tolerates parse
failures per-file: returns an empty AST dict and records the failure
on the caller's error list (best-effort by design — a single broken
file in a 1,050-file repo must not stop the whole ingest).
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Tuple

logger = logging.getLogger(__name__)


def _try_import_hcl2():
    """Lazy import so the engine module loads even without hcl2 installed.

    Returns the hcl2 module or None. UI surface should preflight on this
    being non-None and render a friendly install hint if not.
    """
    try:
        import hcl2  # type: ignore
        return hcl2
    except ImportError:
        return None


def is_hcl_parser_available() -> bool:
    """Preflight helper. UI calls this on page load to gate the form."""
    return _try_import_hcl2() is not None


def parse_file(path: str) -> Tuple[Dict[str, Any], List[str]]:
    """Parse one HCL file. Returns (ast_dict, errors).

    ast_dict shape (per python-hcl2):
        {
            "resource": [{"<tf_type>": {"<name>": {<args>}}}, ...],
            "module":   [{"<name>": {<args>}}, ...],
            "variable": [{"<name>": {<args>}}, ...],
            "output":   [{"<name>": {<args>}}, ...],
            "locals":   [{<key>: <value>, ...}],
            "data":     [{"<tf_type>": {"<name>": {<args>}}}, ...],
            "terraform": [{<args>}],
            "provider":  [{"<name>": {<args>}}, ...],
            ...
        }

    On failure, returns ({}, ["<one-line error>"]).
    """
    hcl2 = _try_import_hcl2()
    if hcl2 is None:
        return ({}, [f"python-hcl2 not installed; cannot parse {path}"])

    try:
        with open(path, "r", encoding="utf-8") as fh:
            ast = hcl2.load(fh)
        return (ast or {}, [])
    except FileNotFoundError as e:
        return ({}, [f"file not found: {path}: {e}"])
    except Exception as e:  # noqa: BLE001 -- best-effort; record + continue
        # python-hcl2 raises a variety of lark/parser exceptions we
        # don't want to type-couple to. Single-line error preserves
        # readability in the UI.
        logger.warning("hcl_parse_failed", extra={"path": path, "error": str(e)})
        return ({}, [f"parse failed: {path}: {type(e).__name__}: {e}"])


def extract_resource_blocks(ast: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Flatten the python-hcl2 resource shape into one record per resource.

    python-hcl2 emits each resource block as
        {"<tf_type>": {"<name>": {<args>}}}
    nested under `ast["resource"]`. We flatten to:
        [{"tf_type": ..., "name": ..., "arguments": {...}}, ...]
    """
    out: List[Dict[str, Any]] = []
    blocks = ast.get("resource", []) or []
    for block in blocks:
        if not isinstance(block, dict):
            continue
        for tf_type, named in block.items():
            if not isinstance(named, dict):
                continue
            for name, args in named.items():
                if not isinstance(args, dict):
                    args = {}
                out.append({
                    "tf_type": tf_type,
                    "name": name,
                    "arguments": args,
                })
    return out


def extract_module_blocks(ast: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Flatten module blocks similarly to resource blocks.

    python-hcl2 shape: ast["module"] = [{"<name>": {<args>}}, ...]
    """
    out: List[Dict[str, Any]] = []
    blocks = ast.get("module", []) or []
    for block in blocks:
        if not isinstance(block, dict):
            continue
        for name, args in block.items():
            if not isinstance(args, dict):
                args = {}
            out.append({"name": name, "arguments": args})
    return out
