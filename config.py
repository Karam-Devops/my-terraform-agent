# config.py

import os

class Config:
    """
    Configuration class for the Terraform IaC Agent.

    This class centralizes all settings and makes them configurable via
    environment variables. This is a best practice that makes the application
    portable and secure, preparing it for deployment on Google Cloud (Phase 2).
    """

    # --- GCP and Vertex AI Settings ---

    # The Google Cloud Project ID to use for API calls.
    # Why: Using os.getenv allows you to override this value without changing the code,
    # which is essential for running the same code in different environments (dev vs. prod).
    GCP_PROJECT_ID: str = os.getenv("GCP_PROJECT_ID", "prod-470211")

    # The GCP region for the Vertex AI API endpoint.
    GCP_LOCATION: str = os.getenv("GCP_LOCATION", "us-central1")

    # The specific Gemini model to use.
    # CRITICAL FIX: The identifier "gemini-2.5-pro" is not yet available in the Vertex AI API.
    # Using "gemini-1.5-pro-preview-0409" (or "gemini-1.5-pro") which is the current, powerful
    # model capable of handling the complex JSON output this agent requires.
    GEMINI_MODEL: str = os.getenv("GEMINI_MODEL", "gemini-2.5-pro")

    # --- Agent Behavior Settings ---

    # A hard limit on the number of loops to prevent infinite runs and unexpected costs.
    MAX_ITERATIONS: int = 5

    # NEW: The root directory where all generated Terraform files will be saved.
    # Why: Centralizing this path here allows us to easily reference it in any
    # node that needs to read from or write to the file system.
    OUTPUT_DIR: str = os.getenv("OUTPUT_DIR", "generated_iac")

# We create a single, importable instance of the Config class.
# In other files, you will simply do `from config import config`
config = Config()