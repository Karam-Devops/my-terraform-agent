"""SRE engine — public entry point.

A+D contract (parity with importer.run.run_workflow,
migrator.run.run_migration, detector.rescan.rescan, policy.scan.scan):

  * RAISES PreflightError on input/environment failures (no alert
    payload, missing project_id, lookback out of range).
  * RETURNS IncidentResult on every completed run, regardless of
    per-source outcomes. A source that 500s shows up as
    SourceTiming(status="failed") in the result, not a raised
    exception — operators still see the partial picture.

Streamlit page (``app/pages/7_🚨_SRE_Agent.py``) calls
``run_incident_triage()`` directly with an ``AlertEnvelope`` pulled
from Cloud Monitoring → Pub/Sub. A CLI/cron path can do the same.

Phase 0 scope
-------------
Day 1 (this commit): orchestrator scaffold + result shape. Source
pulls and hypothesis ranking are stubbed — they return empty lists
but the right dataclasses, so the UI page can render against real
shapes from day one.

Day 2: fills in ``sre.sources.gcp_asset_changes``, ``gcp_iam_changes``,
``gcp_deploys`` + correlator (``sre.correlator``).

Day 3: fills in ``sre.hypothesis`` (Claude API call to write the
ranked-cause narrative).

Until those modules land, the imports below are conditional — calling
``run_incident_triage()`` produces a valid IncidentResult with empty
evidence / no hypotheses + a note explaining the stub.
"""

from __future__ import annotations

import datetime
import os
import time
from typing import Optional

from common.errors import PreflightError
from common.logging import get_logger

from .results import (
    AlertEnvelope,
    IncidentResult,
    SourceTiming,
)


_log = get_logger(__name__)


# ----------------------------------------------------------------------------
# Tunables. Kept module-level (not config-file) so they're easy to scan +
# override via env var without touching code. Same pattern Migrator uses
# for MIGRATOR_OUTPUT_DIRNAME etc.
# ----------------------------------------------------------------------------

# Lookback window the engine collects evidence for. 60 min is the default
# because that's the "what changed in the last hour" use case the user
# called out as 60% of customer value. Operators can override via the
# request (see ``lookback_min`` arg) or via the SRE_DEFAULT_LOOKBACK env.
SRE_DEFAULT_LOOKBACK_MIN = int(os.environ.get("SRE_DEFAULT_LOOKBACK_MIN", "60"))

# Hard caps on lookback. Below 5 min, we'd miss the slow-rolling deploys
# that take a couple of minutes to manifest. Above 6h, Asset Inventory
# starts paginating heavily and the LLM hypothesis context blows past
# Claude's sweet spot. Operators who genuinely need a 24h scrub should
# use the Detector engine (drift) not the SRE agent (incident triage).
SRE_MIN_LOOKBACK_MIN = 5
SRE_MAX_LOOKBACK_MIN = 360

# Sources we attempt to query in Phase 0. Order matters for the UI source
# chips: asset changes first (most relevant for "what changed"), IAM
# second (privilege-escalation incidents), deploys third (rollout-caused
# incidents). Day 2 wires these names to actual collector functions.
SRE_PHASE0_SOURCES = (
    "gcp_asset_changes",
    "gcp_iam_changes",
    "gcp_deploys",
)


def run_incident_triage(
    alert: AlertEnvelope,
    *,
    project_id: str,
    lookback_min: Optional[int] = None,
    tenant_id: str = "default",
) -> IncidentResult:
    """End-to-end incident triage.

    Args:
        alert: Normalized incoming alert (from Pub/Sub puller or any
            future trigger). REQUIRED — the engine never auto-discovers
            an incident; it triages a specific one.
        project_id: GCP project whose audit logs / asset changes get
            scanned. Today this matches the alert's project, but the
            arg is separate so cross-project triage (alert fires in
            prod, evidence pulled from prod+staging) becomes a one-line
            change later.
        lookback_min: How far back to collect evidence, in minutes.
            Defaults to ``SRE_DEFAULT_LOOKBACK_MIN`` (60). Bounded to
            [SRE_MIN_LOOKBACK_MIN, SRE_MAX_LOOKBACK_MIN]; values outside
            raise PreflightError so the UI shows a clear error
            instead of silently clamping.
        tenant_id: SaaS context for structured logging + per-tenant
            snapshot persistence. Defaults to "default" for local dev.

    Returns:
        IncidentResult with alert, evidence, hypotheses, timings, and
        bookkeeping populated. Errors accumulate in ``result.errors``;
        callers render those in the UI banner. Source-level failures
        appear as ``SourceTiming(status="failed")``.

    Raises:
        PreflightError: missing alert, bad project_id, lookback out of
            range. Streamlit page catches and renders ``.user_hint``.
    """
    # ---- preflight ----
    if alert is None:
        raise PreflightError(
            "run_incident_triage() called without an alert",
            stage="validate_alert",
            reason="missing_alert",
        )
    if not getattr(alert, "alert_id", None):
        raise PreflightError(
            "alert.alert_id is required",
            stage="validate_alert",
            reason="missing_alert_id",
        )
    if not project_id:
        raise PreflightError(
            "run_incident_triage() called without project_id",
            stage="validate_project_id",
            reason="missing_project_id",
        )

    effective_lookback = lookback_min or SRE_DEFAULT_LOOKBACK_MIN
    if not (SRE_MIN_LOOKBACK_MIN <= effective_lookback <= SRE_MAX_LOOKBACK_MIN):
        raise PreflightError(
            f"lookback_min={effective_lookback} is outside the allowed "
            f"range [{SRE_MIN_LOOKBACK_MIN}, {SRE_MAX_LOOKBACK_MIN}]",
            stage="validate_lookback",
            reason="lookback_out_of_range",
        )

    log = _log.bind(
        alert_id=alert.alert_id,
        alert_source=alert.source,
        severity=alert.severity,
        project_id=project_id,
        tenant_id=tenant_id,
        lookback_min=effective_lookback,
    )
    log.info("sre_triage_start")
    started = time.monotonic()
    started_iso = _utc_iso_now()

    result = IncidentResult(
        alert=alert,
        project_id=project_id,
        tenant_id=tenant_id,
        lookback_min=effective_lookback,
        started_at=started_iso,
    )

    # ---- collect evidence (Day 2 fills these in) ----
    #
    # Each source is wrapped in its own try/except so a single broken
    # collector (e.g., Cloud Asset API quota, IAM logs not enabled)
    # degrades to status="failed" on that one chip — the rest of the
    # triage still completes. This matches the per-engine-tier
    # philosophy Migrator uses for validation.
    for source_name in SRE_PHASE0_SOURCES:
        source_started = time.monotonic()
        try:
            items = _collect_source(
                source_name=source_name,
                alert=alert,
                project_id=project_id,
                lookback_min=effective_lookback,
            )
            result.evidence.extend(items)
            result.source_timings.append(SourceTiming(
                source=source_name,
                item_count=len(items),
                duration_ms=int((time.monotonic() - source_started) * 1000),
                status="ok",
            ))
            log.info(
                "sre_source_complete",
                source=source_name,
                item_count=len(items),
            )
        except NotImplementedError as stub:
            # Day-1 path: source module exists as a stub. Record it as
            # "partial" with a one-line note so the demo + smoke test
            # show what's coming, not what's broken.
            result.source_timings.append(SourceTiming(
                source=source_name,
                item_count=0,
                duration_ms=int((time.monotonic() - source_started) * 1000),
                status="partial",
                error=f"source not yet implemented: {stub}",
            ))
            result.notes.append(f"{source_name}: stub (Day 2 deliverable)")
            log.info("sre_source_stubbed", source=source_name)
        except Exception as src_err:  # noqa: BLE001
            # Real failure path: wrap, log, keep going.
            result.source_timings.append(SourceTiming(
                source=source_name,
                item_count=0,
                duration_ms=int((time.monotonic() - source_started) * 1000),
                status="failed",
                error=str(src_err),
            ))
            result.errors.append(f"{source_name}: {src_err}")
            log.warning(
                "sre_source_failed",
                source=source_name,
                error=str(src_err),
            )

    log.info(
        "sre_evidence_collected",
        evidence_count=len(result.evidence),
        source_count=len(result.source_timings),
    )

    # ---- correlate + rank (Day 2 + Day 3 fill these in) ----
    #
    # Correlator scores each evidence item against the alert (temporal
    # proximity + resource overlap + change-type weight) and produces
    # ranked Hypothesis objects. LLM step writes the narrative.
    try:
        result.hypotheses = _correlate_and_rank(
            alert=alert,
            evidence=result.evidence,
        )
        log.info(
            "sre_ranking_complete",
            hypothesis_count=len(result.hypotheses),
            top_confidence=(
                result.top_hypothesis.confidence
                if result.top_hypothesis else None
            ),
        )
    except NotImplementedError as stub:
        result.notes.append(f"correlator: stub (Day 2/3 deliverable): {stub}")
        log.info("sre_ranking_stubbed")
    except Exception as rank_err:  # noqa: BLE001
        result.errors.append(f"correlator: {rank_err}")
        log.warning("sre_ranking_failed", error=str(rank_err))

    # ---- LLM narrative rewrite (Day 3) ----
    #
    # Heuristic ranks + scores + cited_evidence stay as the correlator
    # produced them; only the human-facing headline + reasoning bullets
    # get rewritten in operator-grade prose. One LLM call per triage,
    # rank-keyed so a misordered LLM response still maps correctly.
    # Falls back to the heuristic templates on any LLM failure — the
    # triage always ships, just with less polished prose if the LLM
    # plumbing isn't available.
    if result.hypotheses:
        try:
            from .llm.hypothesis_writer import rewrite as _llm_rewrite
            rewritten = _llm_rewrite(
                alert=alert,
                hypotheses=result.hypotheses,
                evidence=result.evidence,
            )
            # Sanity: rewrite() always returns a same-length list (deep
            # copies + selectively mutated headlines). If something
            # weird happened, keep the original.
            if len(rewritten) == len(result.hypotheses):
                result.hypotheses = rewritten
                log.info("sre_llm_rewrite_applied",
                         hypothesis_count=len(result.hypotheses))
            else:
                log.warning("sre_llm_rewrite_length_mismatch",
                            before=len(result.hypotheses),
                            after=len(rewritten))
        except Exception as llm_err:  # noqa: BLE001
            # Belt-and-braces: rewrite() catches its own exceptions and
            # returns the heuristic copies. This outer except is the
            # safety net if a future code path raises pre-fallback.
            result.notes.append(f"llm_rewrite: skipped ({llm_err})")
            log.warning("sre_llm_rewrite_failed", error=str(llm_err))

    # ---- finalize ----
    result.completed_at = _utc_iso_now()
    result.duration_s = round(time.monotonic() - started, 2)

    log.info("sre_triage_complete", **result.as_fields())

    # Best-effort snapshot persistence — mirrors migrator/detector
    # patterns. Never blocks engine completion.
    _persist_best_effort(result, log=log)

    return result


# ----------------------------------------------------------------------------
# Internal dispatch.
#
# Kept as a tiny indirection so Day 2 can drop in source modules without
# touching the orchestrator. Each source module exports
# ``collect(alert, project_id, lookback_min) -> List[EvidenceItem]``;
# the dispatch table below maps the name to the callable.
#
# Until those land, every name raises NotImplementedError, which the
# orchestrator handles as a "partial" timing — not a failure.
# ----------------------------------------------------------------------------


def _collect_source(
    *,
    source_name: str,
    alert: AlertEnvelope,
    project_id: str,
    lookback_min: int,
):
    """Dispatch to a per-source collector. Stub today; Day 2 wires up."""
    if source_name == "gcp_asset_changes":
        try:
            from .sources import gcp_asset_changes  # noqa: F401 — Day 2
        except ImportError as e:
            raise NotImplementedError(
                f"gcp_asset_changes collector not yet wired ({e})"
            )
        if not hasattr(gcp_asset_changes, "collect"):
            raise NotImplementedError("gcp_asset_changes.collect() pending")
        return gcp_asset_changes.collect(
            alert=alert, project_id=project_id, lookback_min=lookback_min,
        )

    if source_name == "gcp_iam_changes":
        try:
            from .sources import gcp_iam_changes  # noqa: F401 — Day 2
        except ImportError as e:
            raise NotImplementedError(
                f"gcp_iam_changes collector not yet wired ({e})"
            )
        if not hasattr(gcp_iam_changes, "collect"):
            raise NotImplementedError("gcp_iam_changes.collect() pending")
        return gcp_iam_changes.collect(
            alert=alert, project_id=project_id, lookback_min=lookback_min,
        )

    if source_name == "gcp_deploys":
        try:
            from .sources import gcp_deploys  # noqa: F401 — Day 2
        except ImportError as e:
            raise NotImplementedError(
                f"gcp_deploys collector not yet wired ({e})"
            )
        if not hasattr(gcp_deploys, "collect"):
            raise NotImplementedError("gcp_deploys.collect() pending")
        return gcp_deploys.collect(
            alert=alert, project_id=project_id, lookback_min=lookback_min,
        )

    # New source name added to SRE_PHASE0_SOURCES without a dispatch
    # branch → loud failure here is correct (the orchestrator catches
    # it and records status="failed", but we want the developer to
    # notice they forgot to wire the dispatch).
    raise ValueError(f"unknown SRE source: {source_name}")


def _correlate_and_rank(
    *,
    alert: AlertEnvelope,
    evidence,
):
    """Score evidence + produce Hypothesis list. Stub today; Day 2/3 fill in."""
    try:
        from . import correlator  # noqa: F401 — Day 2
    except ImportError as e:
        raise NotImplementedError(
            f"correlator not yet wired ({e})"
        )
    if not hasattr(correlator, "rank"):
        raise NotImplementedError("correlator.rank() pending")
    return correlator.rank(alert=alert, evidence=evidence)


# ----------------------------------------------------------------------------
# Persistence + helpers
# ----------------------------------------------------------------------------


def _persist_best_effort(result: IncidentResult, *, log) -> None:
    """Write per-engine snapshot + per-tenant result blob. Never raises."""
    # 1. Shared snapshot pattern (importer/detector/policy/migrator) —
    #    writes <bucket>/snapshots/sre/latest.json + history/<ts>.json.
    try:
        from common.snapshots import write_snapshot
        write_snapshot(
            "sre",
            result.as_fields(),
            result.project_id or "unknown",
            tenant_id=result.tenant_id,
        )
    except Exception as snap_err:  # noqa: BLE001 -- best-effort
        log.warning(
            "snapshot_write_skipped",
            engine="sre",
            error=str(snap_err),
            reason="snapshot persistence failed; engine result unaffected",
        )

    # 2. Per-user result persistence so the page can re-hydrate after
    #    a refresh (mirrors Migrator's save_result). Day 1 wires the
    #    same gs:// / file:// backend Migrator already uses; if the
    #    sre.output.result_persistence module isn't there yet, skip
    #    quietly — the engine return value is the source of truth
    #    inside the running Streamlit session either way.
    try:
        from .output import result_persistence as _persist
        _env_tenant = os.environ.get("MIGRATOR_TENANT_ID")  # same IAP-injected key
        user_key = "::".join(filter(None, [
            _env_tenant, result.tenant_id, result.project_id,
        ])) or "default"
        backend_url = os.environ.get("SRE_PERSIST_BACKEND") \
                      or os.environ.get("MIGRATOR_PERSIST_BACKEND")
        destination = (
            backend_url.rstrip("/") + "/" + user_key.replace("::", "/")
            if backend_url else None
        )
        _persist.save_result(result, user_key=user_key, destination=destination)
    except (ImportError, AttributeError):
        # Day 1: result_persistence module not yet present. Snapshot
        # (above) is enough for the demo path.
        pass
    except Exception as persist_err:  # noqa: BLE001 -- best-effort
        log.warning(
            "result_persist_skipped",
            engine="sre",
            error=str(persist_err),
            reason="UI will not be able to recover this run after refresh; "
                   "engine result unaffected.",
        )


def _utc_iso_now() -> str:
    """ISO-8601 UTC stamp matching the rest of the platform's logs."""
    return datetime.datetime.now(datetime.timezone.utc).isoformat(
        timespec="seconds"
    )
