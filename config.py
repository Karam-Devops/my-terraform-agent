# config.py

# --- IMPORTANT ---
# This is the actual Google Cloud Project ID
GCP_PROJECT_ID = "prod-470211"
GCP_LOCATION = "us-central1"

# This is the model we will use. When new models like Gemini 2.x are available,
# you can simply update this string.
GEMINI_MODEL = "gemini-2.5-pro"

# --- Safety Settings ---
MAX_ITERATIONS = 5 # Set a hard limit on the number of self-correction loops