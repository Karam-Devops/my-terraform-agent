"""mtagent SRE / Incident Response Agent — Phase 8.

Triage + diagnose layer for the existing mtagent platform. Plugs into
the same engine pattern other engines use:

  * ``sre.run.run_incident_triage()`` is the public entry point.
  * Returns ``IncidentResult`` on every completed run; raises
    ``PreflightError`` on input/environment failures.
  * Per-tenant snapshot persistence via the same gs:// / file:// backend
    Migrator uses (``migrator.output.result_persistence``).

The trigger surface is GCP Cloud Monitoring → Pub/Sub today; webhook
sources (PagerDuty, Opsgenie, Datadog) land in a later phase.
"""
