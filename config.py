# config.py
"""
Centralised configuration for the Terraform IaC Agent.

Every setting is env-overridable so the same code runs across dev, prod, and CI
without touching the source. New entries should follow the same pattern:
sensible default in code, override via environment variable.
"""

import os


class Config:
    # ---------------------------------------------------------------------
    # GCP / Vertex AI
    # ---------------------------------------------------------------------

    GCP_PROJECT_ID: str = os.getenv("GCP_PROJECT_ID", "prod-470211")
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
