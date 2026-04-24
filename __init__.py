# my-terraform-agent/__init__.py
"""
Top-level package shell for the Terraform IaC Agent.

Why this file does anything at all
----------------------------------
The intended invocation is `python -m my-terraform-agent.<sub>.<module>`
from one level up (e.g. `python -m my-terraform-agent.translator.run`).
That makes `translator`, `importer`, `detector`, `policy`, `common` all
sub-packages of `my-terraform-agent`.

But several of those modules use bare absolute imports for sibling
packages — e.g. `translator/config.py` does
``from common.terraform_path import resolve_terraform_path`` — written
back when the project was always run from inside its own directory and
`common/` was therefore on `sys.path` directly.

Under the `python -m my-terraform-agent.<sub>` invocation, the parent
dir (one level up) is on `sys.path` instead, and bare `common` doesn't
resolve. The bootstrap below adds *this* package's own directory to
`sys.path` so the sibling-name absolute imports keep working under both
invocation modes:

  - CLI:   `python -m my-terraform-agent.translator.run` (parent on path
           + this bootstrap injects the package dir).
  - Tests: `python -m unittest detector.tests.test_X` from inside the
           repo (the dir is *already* on sys.path as cwd; the bootstrap
           is a no-op because the path is already present).

Idempotent — guarded against double-injection if anything else imports
this package twice. Cheap on first load (one stat-equivalent operation).
"""

import os as _os
import sys as _sys

_HERE = _os.path.dirname(_os.path.abspath(__file__))
if _HERE not in _sys.path:
    _sys.path.insert(0, _HERE)
