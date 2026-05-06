"""Migrator ingest layer.

Three responsibilities:
  * repo_walker — locate IaC files in a directory tree
  * hcl_parser  — parse a single .tf / .hcl file into AST dicts
  * inventory   — assemble walker + parser output into a DiscoveredResource list
"""
