# llm_provider.py
"""
Single source of truth for LLM clients.

Two singletons today:
  - llm       : JSON-mode, deterministic (temperature=0). For structured output.
  - llm_text  : raw-text mode, slight temperature for code-gen variety.

Both are pinned to `config.GEMINI_MODEL`. A future task-keyed router
(Mini-PR 0b) will key clients per-task so cheap/fast models can serve narrow
post-skeleton polish jobs while Pro is reserved for full synthesis. This file
is the seam where that change happens.
"""

import vertexai
from langchain_google_vertexai import ChatVertexAI

from .config import config

# --- 1. Initialize Vertex AI SDK ----------------------------------------
print("--- Initializing Vertex AI SDK ---")
print(f"Project: {config.GCP_PROJECT_ID}, Location: {config.GCP_LOCATION}")
try:
    vertexai.init(project=config.GCP_PROJECT_ID, location=config.GCP_LOCATION)
    print("Vertex AI SDK initialized successfully.")
except Exception as e:
    print(f"CRITICAL ERROR: Failed to initialize Vertex AI SDK. {e}")


# --- 2. JSON-mode client (structured output) ----------------------------
print(
    f"--- Creating JSON LLM client for model: {config.GEMINI_MODEL} "
    f"(retries={config.LLM_MAX_RETRIES}) ---"
)
llm = ChatVertexAI(
    model_name=config.GEMINI_MODEL,
    temperature=0.0,
    max_retries=config.LLM_MAX_RETRIES,
    model_kwargs={
        "response_format": {"type": "json_object"},
        "convert_system_message_to_human": True,
    },
)
print("JSON LLM client ready.")


def get_llm_client():
    """Returns the pre-initialized JSON-mode LLM client."""
    return llm


# --- 3. Text-mode client (free-form code gen) ---------------------------
print(f"--- Creating text LLM client for model: {config.GEMINI_MODEL} ---")
llm_text = ChatVertexAI(
    model_name=config.GEMINI_MODEL,
    temperature=0.05,  # tiny temperature helps code-gen variety
    max_retries=config.LLM_MAX_RETRIES,
    # No response_format -> raw text output.
)
print("Text LLM client ready.")


def get_llm_text_client():
    """Returns the pre-initialized raw-text LLM client."""
    return llm_text