# config.py
"""
Centralised configuration for the Terraform IaC Agent.

Every setting is env-overridable so the same code runs across dev, prod, and CI
without touching the source. New entries should follow the same pattern:
sensible default in code, override via environment variable.
"""

import os
from typing import Optional


class Config:
    # ---------------------------------------------------------------------
    # GCP project IDs — host vs target vs demo
    # ---------------------------------------------------------------------
    #
    # Three concepts, strictly separated. Today (single dev project) they
    # all collapse to the same value, but the upcoming company-host /
    # client-target split makes the conflation dangerous, so we tease
    # them apart now while it's cheap.
    #
    #   HOST_PROJECT_ID    — where the SaaS itself runs (Vertex AI, Cloud
    #                        Run, secrets). Read at startup by
    #                        llm_provider.py. Same in dev/staging/prod for
    #                        a given deployment; never per-tenant.
    #
    #   TARGET_PROJECT_ID  — which project the importer/detector SCANS.
    #                        Per-session — the user can override at the
    #                        prompt. In dev = your own GCP; in prod =
    #                        the client's GCP. Optional at config-load
    #                        time (the prompt can supply it instead).
    #
    #   DEMO_PROJECT_ID    — optional safety lock. When set, the resolver
    #                        REFUSES to scan any project other than this
    #                        one, regardless of what the user types or
    #                        what TARGET_PROJECT_ID says. Use during
    #                        vendor demos and client-onboarding sessions
    #                        where a fat-finger scan of the wrong project
    #                        is a cardinal sin. Unset to scan freely.
    #
    # Back-compat: the legacy GCP_PROJECT_ID env var still works. Its
    # historical use was Vertex AI init (in llm_provider.py), so we map
    # it to HOST_PROJECT_ID. Existing deployments don't need to change
    # their env. New code should reach for HOST_PROJECT_ID /
    # TARGET_PROJECT_ID explicitly so intent is obvious at the call site.

    HOST_PROJECT_ID: str = os.getenv(
        "HOST_PROJECT_ID",
        os.getenv("GCP_PROJECT_ID", "prod-470211"),
    )
    TARGET_PROJECT_ID: Optional[str] = (
        os.getenv("TARGET_PROJECT_ID")
        or os.getenv("GCP_PROJECT_ID")  # legacy fallback
    )
    DEMO_PROJECT_ID: Optional[str] = os.getenv("DEMO_PROJECT_ID")

    # Back-compat alias. llm_provider.py and main.py reach for this name;
    # leaving it in place means we don't have to change those files in
    # this PR. New code should NOT use this — it's intentionally vague
    # about whether you mean host or target.
    GCP_PROJECT_ID: str = HOST_PROJECT_ID

    GCP_LOCATION: str = os.getenv("GCP_LOCATION", "us-central1")

    # The Gemini model identifier sent to Vertex AI.
    #
    # Default `gemini-2.5-pro` is an *alias* that resolves to the current GA
    # build. For reproducible enterprise runs, override with a dated build,
    # e.g.
    #     GEMINI_MODEL=gemini-2.5-pro-002
    # Run `gcloud ai models list --region=$GCP_LOCATION` to see available IDs
    # in your region.
    GEMINI_MODEL: str = os.getenv("GEMINI_MODEL", "gemini-2.5-pro")

    # Fallback model used when the primary fails (auth blip, throttle,
    # malformed JSON). Cheaper / faster — a degraded but working agent beats a
    # dead one. Wired in by the upcoming router (Mini-PR 0b); declared here so
    # callers can already read it.
    GEMINI_MODEL_FALLBACK: str = os.getenv(
        "GEMINI_MODEL_FALLBACK", "gemini-2.5-flash"
    )

    # Number of retries the LLM client performs on transient errors.
    LLM_MAX_RETRIES: int = int(os.getenv("LLM_MAX_RETRIES", "3"))

    # Per-request timeout in seconds. Read by the router PR; left here so the
    # knob is documented in one place.
    LLM_TIMEOUT_SECONDS: int = int(os.getenv("LLM_TIMEOUT_SECONDS", "120"))

    # ---------------------------------------------------------------------
    # Agent behaviour
    # ---------------------------------------------------------------------

    # Hard cap on agent loops to bound cost on runaway runs.
    MAX_ITERATIONS: int = int(os.getenv("MAX_ITERATIONS", "5"))

    # Root directory for generated Terraform files.
    OUTPUT_DIR: str = os.getenv("OUTPUT_DIR", "generated_iac")


# Single importable instance: `from config import config`
config = Config()


def resolve_target_project_id(user_supplied: Optional[str]) -> str:
    """Resolve and validate the project ID the importer/detector should scan.

    Resolution order:
      1. If `user_supplied` is non-empty (typically from an interactive
         prompt), use it. Whitespace is stripped.
      2. Otherwise fall back to `config.TARGET_PROJECT_ID` (which itself
         falls back to the legacy `GCP_PROJECT_ID` env var).
      3. If `config.DEMO_PROJECT_ID` is set, the resolved ID MUST equal
         it — any mismatch is a hard error. This is the safety lock for
         vendor demos and client-onboarding sessions.

    Returns the resolved project ID string.

    Raises:
        ValueError: when nothing could be resolved, or when the demo-lock
                    check fails. Callers should catch this and print the
                    message to the user — do NOT swallow silently, the
                    whole point is to make accidental scans loud.

    The resolver lives at module scope (not on Config) so it can be
    imported and tested standalone without instantiating Config or
    monkey-patching env vars at instance level.
    """
    chosen = (user_supplied or "").strip() or config.TARGET_PROJECT_ID
    if not chosen:
        raise ValueError(
            "No GCP project ID supplied. Set TARGET_PROJECT_ID (or the "
            "legacy GCP_PROJECT_ID) in the environment, or provide one "
            "at the prompt."
        )
    if config.DEMO_PROJECT_ID and chosen != config.DEMO_PROJECT_ID:
        raise ValueError(
            f"DEMO_PROJECT_ID safety lock engaged: refusing to scan "
            f"{chosen!r}. Only {config.DEMO_PROJECT_ID!r} is permitted "
            f"in this environment. Unset DEMO_PROJECT_ID to scan other "
            f"projects."
        )
    return chosen
