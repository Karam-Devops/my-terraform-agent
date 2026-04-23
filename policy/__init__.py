# policy/__init__.py
"""
Policy enforcer module — fourth stage of the pipeline.

Wraps OPA/Conftest to evaluate Rego policies against:
  1. Live cloud snapshots (continuous compliance / standalone scan)
  2. Drifted resources (decoration on the detector report)
  3. terraform-show-json plan output (pre-apply gate — deferred to step 2)

Public entry points:
  python -m policy.run                  # standalone compliance scan
  policy.integration.classify_drift(...) # called by detector

Conftest must be on PATH. See policy/engine.py for install instructions.
"""
