# detector/__init__.py
"""
Bootstraps sys.path so that `from importer import ...` resolves regardless
of the current working directory the user invoked Python from.

Without this, `python -m detector.run` only works if the cwd is the project
root, because cloud_snapshot.py imports the sibling `importer` package by
absolute name. With this shim, the package is robust to any cwd.
"""
import os
import sys

_PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)
