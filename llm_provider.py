# llm_provider.py

import vertexai
from langchain_google_vertexai import ChatVertexAI
from .config import config

# --- 1. Initialize Vertex AI SDK (Best Practice) ---
print("--- Initializing Vertex AI SDK ---")
print(f"Project: {config.GCP_PROJECT_ID}, Location: {config.GCP_LOCATION}")
try:
    vertexai.init(project=config.GCP_PROJECT_ID, location=config.GCP_LOCATION)
    print("Vertex AI SDK initialized successfully.")
except Exception as e:
    print(f"CRITICAL ERROR: Failed to initialize Vertex AI SDK. {e}")

# --- 2. Create a Singleton LLM Client ---
print(f"--- Creating LLM Client for model: {config.GEMINI_MODEL} ---")
llm = ChatVertexAI(
    # Core parameters remain at the top level
    model_name=config.GEMINI_MODEL,
    temperature=0.0,

    # --- THIS IS THE FIX ---
    # All provider-specific arguments are now cleanly placed inside model_kwargs.
    # This aligns with the latest LangChain standards and removes the warnings.
    model_kwargs={
        "response_format": {
            "type": "json_object",
        },
        "convert_system_message_to_human": True
    }
)
print("LLM Client created successfully.")


def get_llm_client():
    """
    Returns the pre-initialized, singleton LLM client.
    """
    return llm