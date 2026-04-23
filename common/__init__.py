# common/__init__.py
"""Shared utilities used across importer / translator / detector / policy.

Currently exposes:
    - terraform_path.resolve_terraform_path()  — single source of truth for
      locating the `terraform` binary.

As we move toward SaaS, this is the natural home for shared concerns
(auth helpers, db session, telemetry, secrets) so we don't keep growing
the per-engine config.py files.
"""
