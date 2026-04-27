# llm_provider.py
"""
Single source of truth for LLM clients.

Two singletons:
  - llm       : JSON-mode, deterministic (temperature=0). For structured output.
  - llm_text  : raw-text mode, slight temperature for code-gen variety.

Both pinned to ``config.GEMINI_MODEL``. A future task-keyed router will
key clients per-task so cheap/fast models can serve narrow post-skeleton
polish jobs while Pro is reserved for full synthesis. This file is the
seam where that change happens.

Lazy initialisation (P3-1)
--------------------------
Pre-P3-1, ``vertexai.init()`` and both ``ChatVertexAI(...)`` constructors
ran at module import time. That meant 3-5 seconds of cold-start latency
on every interpreter boot, paid in the first user-facing request. The
Phase 0 audit flagged this as the canonical ``CC-3 Cold-start preflight``
bug for the Translator engine.

P3-1 moves all init to lazy-on-first-use. The two ``get_*`` accessors
keep their original signatures (backward compat for every importer /
translator / detector call site), and a new ``preflight()`` function
lets a deployment intentionally eager-init at boot:

  * Cloud Run (Phase 5): readiness probe calls ``preflight()`` and
    returns 503 if it raises -> Cloud Run does not route traffic to a
    broken revision.
  * Streamlit (Phase 6): main() calls ``preflight()`` once at startup so
    the first user-facing translation isn't slow.
  * CLI (today): no caller invokes ``preflight()`` -- the first ``get_*``
    call in importer/translator pays the cost. Same as pre-P3-1
    behaviour from the user's perspective, just shifted by one call site.
"""

import vertexai
from langchain_google_vertexai import ChatVertexAI

from .config import config
from .common.errors import PreflightError
from .common.logging import get_logger

_log = get_logger(__name__)


# ---------------------------------------------------------------------------
# Module-level singletons. Populated lazily by _ensure_initialized() and
# the get_*_client() accessors below. The None sentinel is the "uninitialised"
# marker; every accessor checks for None before doing the actual work.
# ---------------------------------------------------------------------------

_vertex_initialized: bool = False
_llm_json_client = None
_llm_text_client = None


def _ensure_vertex_initialized() -> None:
    """Idempotently initialise the Vertex AI SDK.

    Cheap if already done (single boolean check). Raises ``PreflightError``
    on init failure -- callers either let it propagate (translator /
    importer crashes loudly with a typed exception) or catch it and
    surface to a readiness probe (Phase 5).

    Pre-P3-1 this lived as bare module-level code with a try/except that
    swallowed exceptions and proceeded in a half-broken state -- the
    SDK silently absent caused mysterious downstream failures hours
    later. The typed exception forces the failure to happen at the
    boundary where it can be reasoned about.
    """
    global _vertex_initialized
    if _vertex_initialized:
        return
    _log.info("vertex_init_start",
              project=config.GCP_PROJECT_ID,
              location=config.GCP_LOCATION)
    try:
        vertexai.init(
            project=config.GCP_PROJECT_ID,
            location=config.GCP_LOCATION,
        )
    except Exception as e:  # noqa: BLE001 - re-raised as typed
        _log.error("vertex_init_failed",
                   project=config.GCP_PROJECT_ID,
                   location=config.GCP_LOCATION,
                   error_type=type(e).__name__,
                   error=str(e))
        raise PreflightError(
            f"Vertex AI SDK init failed: {e}",
            stage="vertex_ai_init",
            reason=str(e),
        ) from e
    _vertex_initialized = True
    _log.info("vertex_init_ok",
              project=config.GCP_PROJECT_ID,
              location=config.GCP_LOCATION)


def get_llm_client():
    """Return the JSON-mode LLM client. Lazy-init on first call.

    Used by translator's blueprint extraction (Phase 1) where the LLM
    must return strict JSON conforming to a known schema.
    Temperature=0 for full determinism.

    Raises:
        PreflightError: Vertex AI init failed AND this is the first
            access. Subsequent calls re-raise the same condition (the
            SDK isn't initialised; nothing useful to return).
    """
    global _llm_json_client
    if _llm_json_client is None:
        _ensure_vertex_initialized()
        _log.info("llm_client_create_start",
                  mode="json",
                  model=config.GEMINI_MODEL,
                  max_retries=config.LLM_MAX_RETRIES)
        _llm_json_client = ChatVertexAI(
            model_name=config.GEMINI_MODEL,
            temperature=0.0,
            max_retries=config.LLM_MAX_RETRIES,
            model_kwargs={
                "response_format": {"type": "json_object"},
                "convert_system_message_to_human": True,
            },
        )
        _log.info("llm_client_create_ok", mode="json")
    return _llm_json_client


def get_llm_text_client():
    """Return the raw-text LLM client. Lazy-init on first call.

    Used by importer's HCL generator and translator's HCL generator
    (Phase 2 of translation). Tiny temperature (0.05) gives the LLM
    a small variety budget for HCL code generation -- empirically
    helpful for avoiding pathological loops where the LLM keeps
    emitting identical broken output across retries.

    Raises:
        PreflightError: same as ``get_llm_client``.
    """
    global _llm_text_client
    if _llm_text_client is None:
        _ensure_vertex_initialized()
        _log.info("llm_client_create_start",
                  mode="text",
                  model=config.GEMINI_MODEL,
                  max_retries=config.LLM_MAX_RETRIES)
        _llm_text_client = ChatVertexAI(
            model_name=config.GEMINI_MODEL,
            temperature=0.05,  # tiny temperature helps code-gen variety
            max_retries=config.LLM_MAX_RETRIES,
            # No response_format -> raw text output.
        )
        _log.info("llm_client_create_ok", mode="text")
    return _llm_text_client


def preflight() -> dict:
    """Eagerly initialise both LLM clients up-front.

    Call ONCE at app boot to pay the ~3-5s cold-start cost intentionally
    instead of in the first user-facing request. Returns a dict suitable
    for a readiness probe to render as JSON status (Phase 5 packaging
    consumes this).

    Three components initialise in dependency order:
        1. Vertex AI SDK     (the project/location handshake)
        2. JSON-mode client  (used by translator Phase 1 blueprint extract)
        3. Text-mode client  (used by importer HCL gen + translator Phase 2)

    Returns:
        dict mapping each component name to "ok" if init succeeded.
        On any failure the function raises PreflightError BEFORE
        constructing the return value -- callers must handle the
        exception to know which component failed.

    Raises:
        PreflightError: any init failure. Exception's ``stage`` field
            distinguishes vertex_ai_init vs LLM-client construction
            (LLM client failures bubble through ``_ensure_vertex_initialized``'s
            stage tag if Vertex isn't ready; otherwise propagate raw).
    """
    result: dict = {
        "vertex_ai": "uninitialized",
        "llm_json_client": "uninitialized",
        "llm_text_client": "uninitialized",
    }
    _ensure_vertex_initialized()
    result["vertex_ai"] = "ok"
    get_llm_client()
    result["llm_json_client"] = "ok"
    get_llm_text_client()
    result["llm_text_client"] = "ok"
    _log.info("preflight_ok", **result)
    return result


# ---------------------------------------------------------------------------
# Back-compat shim: pre-P3-1 callers reached for module-level `llm` and
# `llm_text` bindings directly. New code must use the get_*_client()
# accessors. These property-style globals are deprecated and will be
# removed once we audit all call sites (none today, but defensive).
# ---------------------------------------------------------------------------

# Intentionally NOT exposing `llm` and `llm_text` as module-level globals
# anymore. The pre-P3-1 versions ran at import time, so any direct
# reference would have triggered cold-start anyway. New callers MUST use
# get_llm_client() / get_llm_text_client(). Grep across the codebase
# confirms the only references are via the accessor functions, which
# the lazy refactor preserves with identical signatures.
