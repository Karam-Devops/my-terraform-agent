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

import time

import vertexai
# CC-7 / P3-7 (2026-04-27): ChatVertexAI emits a LangChainDeprecationWarning
# on every invoke ("deprecated in LangChain 3.2.0, will be removed in 4.0.0;
# use langchain-google-genai instead"). The warning's recommended fix is
# WRONG for our architecture: langchain-google-genai is the Google AI
# Studio API client (API-key auth), NOT a Vertex AI client. Our Phase 5
# Cloud Run design relies on Vertex AI + ADC + cross-project SA
# impersonation (host SA -> tenant SA), which Google AI Studio does not
# support.
#
# The genuine migration target is one of:
#   1. A successor class in langchain-google-vertexai itself (the
#      package may have introduced one and the warning forgot to point
#      at it).
#   2. Drop LangChain for the LLM-call layer and use
#      vertexai.generative_models.GenerativeModel directly. We already
#      wrap retry/backoff ourselves (safe_invoke, P3-5); LangChain's
#      value-add for our two-message prompts is marginal.
#
# Decision (P3-7): defer the migration to Phase 5 packaging when
# requirements.txt pinning happens together. The warning is non-fatal
# (LangChain 4.0 has no announced ship date), so we have runway. See
# docs/saas_readiness_punchlist.md CC-7 for the full scope-correction
# note.
from langchain_google_vertexai import ChatVertexAI

from .config import config
from .common.errors import PreflightError, UpstreamTimeout
from .common.logging import get_logger

_log = get_logger(__name__)

# P3-5: tokens that, when present in the str() of a raised exception,
# mark it as "transient -- worth retrying" rather than a permanent
# bug. Heuristic-based (LangChain's exception types vary across
# package versions and we don't want to lock to a specific version's
# internal exception hierarchy). Conservative list -- if a token
# appears here it's well-known industry shorthand for a backoff-able
# condition.
_TRANSIENT_ERROR_TOKENS = (
    "429",                  # HTTP 429 Too Many Requests / quota
    "ResourceExhausted",    # gRPC RESOURCE_EXHAUSTED (Google API style)
    "DeadlineExceeded",     # gRPC DEADLINE_EXCEEDED (timeout)
    "timeout",              # generic timeout in error message
    "timed out",            # generic timeout in error message (verb form)
    "503",                  # HTTP 503 Service Unavailable
    "502",                  # HTTP 502 Bad Gateway (proxy/load balancer hiccup)
    "Unavailable",          # gRPC UNAVAILABLE
)


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
                  max_retries=config.LLM_MAX_RETRIES,
                  timeout_s=config.LLM_TIMEOUT_SECONDS)
        # P3-5: timeout was defined in config.py but never wired
        # to the LangChain client (Phase 0 audit WARN). Without it, the
        # client falls back to the SDK default (no per-request timeout
        # at all on some Vertex AI client versions), so a hung Vertex
        # request could wedge the calling worker indefinitely. The
        # safe_invoke() wrapper below adds an outer retry loop that
        # converts persistent failures to typed UpstreamTimeout for
        # the caller to handle.
        #
        # P4-12: SMOKE 4 surfaced that the original kwarg name
        # `request_timeout` was silently dropped into model_kwargs by
        # newer langchain-google-vertexai (the LangChain layer renamed
        # the parameter to `timeout`). The result: the timeout was
        # NEVER actually applied -- a hung Vertex request would have
        # hung indefinitely despite the apparent "wiring". UserWarning
        # in SMOKE 4 stdout: "Unexpected argument 'request_timeout'
        # provided to ChatVertexAI. Did you mean: 'timeout'?". Renamed
        # to `timeout` per the LangChain warning's hint.
        _llm_json_client = ChatVertexAI(
            model_name=config.GEMINI_MODEL,
            temperature=0.0,
            max_retries=config.LLM_MAX_RETRIES,
            timeout=config.LLM_TIMEOUT_SECONDS,
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
                  max_retries=config.LLM_MAX_RETRIES,
                  timeout_s=config.LLM_TIMEOUT_SECONDS)
        # P3-5 + P4-12: same `timeout` kwarg wiring as the JSON
        # client. See get_llm_client() docstring for the rationale +
        # the rename history.
        _llm_text_client = ChatVertexAI(
            model_name=config.GEMINI_MODEL,
            temperature=0.05,  # tiny temperature helps code-gen variety
            max_retries=config.LLM_MAX_RETRIES,
            timeout=config.LLM_TIMEOUT_SECONDS,
            # No response_format -> raw text output.
        )
        _log.info("llm_client_create_ok", mode="text")
    return _llm_text_client


def _is_transient_error(exc: Exception) -> bool:
    """Heuristic-classify an exception as worth retrying.

    Rather than coupling to a specific LangChain version's exception
    hierarchy (which has changed across the 0.x / 1.x / 2.x / 3.x line),
    we string-match the exception text against a conservative list of
    well-known industry shorthand for transient backoff-able conditions.
    Pure function so it's unit-testable without mocking the LLM client.

    Returns:
        True if the exception's str() representation contains any token
        from _TRANSIENT_ERROR_TOKENS. False otherwise.
    """
    text = str(exc)
    return any(token in text for token in _TRANSIENT_ERROR_TOKENS)


def safe_invoke(client, messages, *, max_attempts: int = None,
                base_delay_s: float = 2.0):
    """Invoke an LLM with exponential backoff on transient failures.

    Wraps ``client.invoke(messages)`` with a retry loop targeting:

      * HTTP 429 / RESOURCE_EXHAUSTED  (quota / rate limit)
      * Timeout / DEADLINE_EXCEEDED    (request didn't return in time)
      * HTTP 502 / 503 / UNAVAILABLE   (upstream / proxy hiccup)

    Non-transient errors (auth failures, malformed input, unknown
    model, bad SDK config) are NOT retried -- they propagate
    immediately so the caller sees the real bug instead of waiting
    for backoff to give up.

    Backoff schedule: ``base_delay_s * (2 ** attempt)`` -- so with the
    default ``base_delay_s=2.0`` the delays are 2s, 4s, 8s, 16s before
    each successive retry attempt. Capped at ``max_attempts`` total
    invocations.

    Args:
        client:        a ChatVertexAI (or compatible) instance with
            an ``.invoke(messages)`` method that returns an object
            with a ``.content`` attribute on success.
        messages:      list of LangChain ``BaseMessage`` instances
            (System + Human + ...) describing the prompt.
        max_attempts:  total invocation count (initial + retries).
            Defaults to ``config.LLM_MAX_RETRIES + 1`` so we get the
            same effective retry budget as ChatVertexAI's built-in
            ``max_retries``, just at our outer layer instead of theirs.
            We re-do the budget at the outer layer because LangChain's
            internal retry doesn't always classify 429 as retryable
            depending on package version (P3-7 migration replaces
            this with a more predictable surface).
        base_delay_s:  base for exponential backoff. Default 2.0s
            chosen to align with the recommended Vertex AI quota
            cool-down on 429 responses.

    Returns:
        The ``client.invoke(messages)`` return value on success.

    Raises:
        UpstreamTimeout: when ``max_attempts`` retries all surface
            transient errors. The original final exception is preserved
            in ``__cause__`` for debug. ``elapsed_s`` and ``timeout_s``
            fields on the raised UpstreamTimeout reflect the wall-clock
            spent across ALL retry attempts (including backoff sleeps),
            so log filters keying off `timeout_s` see the real total
            blast radius rather than just the last attempt's slice.
        Exception: any non-transient exception raised by the underlying
            ``client.invoke()`` propagates unchanged. Caller decides how
            to surface (translator engines today catch broadly).
    """
    if max_attempts is None:
        max_attempts = config.LLM_MAX_RETRIES + 1
    started = time.monotonic()
    last_exc = None
    for attempt in range(1, max_attempts + 1):
        try:
            return client.invoke(messages)
        except Exception as e:  # noqa: BLE001 -- classified below
            last_exc = e
            if not _is_transient_error(e):
                # Permanent error -- don't waste retries / backoff on
                # something that will never succeed.
                _log.warning(
                    "llm_invoke_permanent_error",
                    attempt=attempt,
                    error_type=type(e).__name__,
                    error=str(e),
                )
                raise
            if attempt >= max_attempts:
                # All retries exhausted on transient errors -- caller
                # gets a typed UpstreamTimeout with the real total
                # elapsed time so dashboards filter correctly.
                elapsed = time.monotonic() - started
                _log.error(
                    "llm_invoke_retries_exhausted",
                    attempts=attempt,
                    elapsed_s=round(elapsed, 2),
                    error_type=type(e).__name__,
                    error=str(e),
                )
                raise UpstreamTimeout(
                    f"LLM invoke failed after {attempt} attempts: {e}",
                    binary="vertex_ai",
                    stage="llm_invoke",
                    elapsed_s=round(elapsed, 2),
                    timeout_s=float(config.LLM_TIMEOUT_SECONDS),
                    cmd="llm.invoke",
                ) from e
            # Transient and budget remaining -- backoff and retry.
            sleep_s = base_delay_s * (2 ** (attempt - 1))
            _log.warning(
                "llm_invoke_transient_retry",
                attempt=attempt,
                max_attempts=max_attempts,
                sleep_s=sleep_s,
                error_type=type(e).__name__,
                error=str(e),
            )
            time.sleep(sleep_s)
    # Defensive: should be unreachable -- the loop either returns or
    # raises. Keeps mypy / static analysers happy.
    raise UpstreamTimeout(
        f"LLM invoke unexpectedly fell through retry loop: {last_exc}",
        binary="vertex_ai",
        stage="llm_invoke",
        elapsed_s=round(time.monotonic() - started, 2),
        timeout_s=float(config.LLM_TIMEOUT_SECONDS),
        cmd="llm.invoke",
    ) from last_exc


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
