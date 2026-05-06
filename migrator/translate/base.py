"""Shared dataclasses for the translate layer."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List


@dataclass
class Translation:
    """Output of one resource → AWS translation pass.

    The translator returns rendered HCL for the leaf terragrunt.hcl's
    `inputs = { ... }` block AS A STRING (not a dict) because Terragrunt
    inputs commonly contain function calls, references to `dependency.x`
    outputs, and `local.*` references that must be preserved verbatim
    in HCL — a Python dict round-trip would lose them.

    The terragrunt_emitter splices `aws_inputs_hcl` directly into the
    leaf file, so the translator owns the formatting.
    """
    service_name: str             # e.g. "s3-bucket"; matches AWSModuleSpec.service_name
    aws_inputs_hcl: str           # rendered HCL body to drop into `inputs = { ... }`
    notes: List[str] = field(default_factory=list)


# Shared default versions.tf for AWS modules.
DEFAULT_VERSIONS_TF = """terraform {
  required_version = ">= 1.5.0"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.20"
    }
  }
}
"""


@dataclass
class AWSModuleSpec:
    """The AWS module to emit under target/modules/<service_name>/."""
    service_name: str
    main_tf: str
    variables_tf: str
    outputs_tf: str
    versions_tf: str = DEFAULT_VERSIONS_TF
    readme_md: str = ""


def hcl_format_value(v) -> str:
    """Render a Python value as HCL syntax for embedding in inputs.

    Handles strings (with quoting), bools, numbers, lists, and dicts
    recursively. Strings starting with `$` (interpolations) or
    `dependency.`/`local.`/`var.` (references) are passed through
    UNQUOTED so they evaluate as HCL expressions.
    """
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    if v is None:
        return "null"
    if isinstance(v, str):
        # Detect raw HCL expressions vs string literals.
        s = v.strip()
        if s.startswith("${") and s.endswith("}"):
            # ${dependency.x.outputs.y} or ${local.foo} — keep as
            # a string with interpolation; HCL evaluates inside.
            return f'"{v}"'
        if (s.startswith("dependency.") or s.startswith("local.")
                or s.startswith("var.") or s.startswith("module.")
                or s.startswith("data.") or s.startswith("path.")):
            # Bare HCL reference — render unquoted.
            return v
        # Plain string literal — quote and escape internal quotes.
        escaped = v.replace('\\', '\\\\').replace('"', '\\"')
        return f'"{escaped}"'
    if isinstance(v, list):
        if not v:
            return "[]"
        items = [hcl_format_value(x) for x in v]
        # Heuristic: if any item is multi-line, render multi-line list.
        if any("\n" in x for x in items):
            inner = ",\n".join("    " + x.replace("\n", "\n    ") for x in items)
            return f"[\n{inner}\n  ]"
        return "[" + ", ".join(items) + "]"
    if isinstance(v, dict):
        if not v:
            return "{}"
        lines = ["{"]
        for k, val in v.items():
            rendered = hcl_format_value(val)
            # Quote key if it has hyphens / special chars; bare otherwise.
            key_str = k if k.replace("_", "").replace("-", "").isalnum() and not k[0].isdigit() else f'"{k}"'
            if "-" in k:
                key_str = f'"{k}"'
            if "\n" in rendered:
                indented = rendered.replace("\n", "\n    ")
                lines.append(f"    {key_str} = {indented}")
            else:
                lines.append(f"    {key_str} = {rendered}")
        lines.append("  }")
        return "\n".join(lines)
    # Fallback: stringify and quote.
    return f'"{str(v)}"'
