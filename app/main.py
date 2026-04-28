# app/main.py
"""mtagent Streamlit UI — PSA-2 placeholder.

This is the entrypoint Cloud Run hits via the Dockerfile CMD:

    streamlit run app/main.py --server.port=$PORT --server.address=0.0.0.0

PSA-2 (Phase 5A) ships this as a minimal status page that:
  * Returns HTTP 200 so Cloud Run health checks pass
  * Renders the host project + state bucket env vars so operators
    can verify the deploy is wired correctly
  * Includes a no-op /healthz indicator (Streamlit just needs an
    OK response on / to be healthy)

Phase 6 PUI-1 replaces the body with the multi-page Firefly-parity
shell (Dashboard / Inventory / Codify / Drift / Policy / Settings).
The placeholder keeps Cloud Run live during the gap so we can iterate
on the UI without re-deploying the container every time.
"""

import os

import streamlit as st


st.set_page_config(
    page_title="mtagent",
    page_icon="🚀",
    layout="centered",
    initial_sidebar_state="collapsed",
)

st.title("🚀 mtagent")
st.caption("Multi-cloud Terraform automation — Round-1 SaaS")

st.markdown("---")

st.success("✅ Cloud Run deploy successful")

st.markdown(
    "**Status:** Phase 5A scaffolding online. The full Streamlit UI is "
    "under construction — Phase 6 (PUI-1..PUI-10) replaces this "
    "placeholder with the Firefly-parity multi-page shell."
)

st.markdown("### Runtime configuration")

# Show env vars so operators can verify the deploy picked up the
# right values. Uses .get with explicit default for clarity in the UI
# (vs the actual fallback chain in config.py).
config_rows = [
    ("Host project", os.environ.get("HOST_PROJECT_ID", "(unset)")),
    ("State bucket", f"gs://{os.environ.get('MTAGENT_STATE_BUCKET', '(unset)')}/"),
    ("Region", os.environ.get("GCP_LOCATION", "(unset)")),
    ("Translator targets", os.environ.get("TRANSLATOR_TARGETS_ALLOWED", "(unset)")),
    ("Persist blueprints", os.environ.get("MTAGENT_PERSIST_BLUEPRINTS", "(unset)")),
    ("Auto-quarantine", os.environ.get("IMPORTER_AUTO_QUARANTINE", "(unset)")),
    ("Max translation workers", os.environ.get("MAX_TRANSLATION_WORKERS", "(unset)")),
    ("Import base", os.environ.get("MTAGENT_IMPORT_BASE", "(unset)")),
]

st.table({"Setting": [r[0] for r in config_rows],
          "Value": [r[1] for r in config_rows]})

st.markdown("---")

st.caption(
    "Built from `app/main.py` (PSA-2 placeholder). Replace this body "
    "during Phase 6 PUI-1 with the Streamlit multi-page shell."
)
