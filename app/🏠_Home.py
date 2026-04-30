# app/🏠_Home.py
"""mtagent Streamlit home page (PUI-1, renamed from main.py in PUI-5f).

Entry point Cloud Run hits via the Dockerfile CMD:

    streamlit run app/🏠_Home.py --server.port=$PORT --server.address=0.0.0.0

Streamlit treats this file (the entry script) as the home page and
auto-discovers files under ``app/pages/`` as additional pages, ordered
by their numeric prefix and shown in the sidebar nav. Each page calls
``render_sidebar()`` from ``app.ui.sidebar`` so the global project
picker is consistent everywhere.

PUI-5f rename rationale: pre-rename the entry script was ``app/main.py``
which Streamlit displayed in the sidebar as plain ``main`` -- visually
out of place next to the emoji-prefixed engine pages. Renaming to
``🏠_Home.py`` makes the sidebar consistent (🏠 Home as the landing).

Why this file is small: the per-engine work lives in the page files
(``app/pages/N_*.py``). Keeping the entry minimal means the cold-start
import cost is just Streamlit + the sidebar helper -- engine modules
are imported lazily by the pages that need them.
"""

import streamlit as st

from app.ui.sidebar import render_sidebar
from app.ui.theme import apply_theme_polish


st.set_page_config(
    page_title="mtagent",
    page_icon="🚀",
    layout="wide",
    initial_sidebar_state="expanded",
)

# PUI-1B v3.4: page-wide CSS polish (Firefly-inspired). Runs after
# set_page_config (Streamlit requirement) and before any content.
apply_theme_polish()

# Render the sidebar (project picker + runtime info). Returns the
# selected project_id, but the landing page itself doesn't use it
# directly -- it's persisted to st.session_state for the per-engine
# pages to read.
render_sidebar()

st.title("🚀 mtagent")
st.caption("Multi-cloud Terraform automation — Round-1 SaaS POC")

st.markdown("---")

st.markdown(
    "**Pick an engine from the left sidebar** to get started. "
    "Each engine reads from / writes to the per-project workdir "
    "stored in GCS, so all four engines see a consistent view."
)

# Quick orientation grid -- mirrors the page nav so first-time
# operators see what each engine does at a glance.
col1, col2 = st.columns(2)
with col1:
    st.markdown("#### 📦 Inventory")
    st.caption(
        "Discover GCP resources and generate Terraform code. "
        "Run this first on a new project."
    )
    st.markdown("#### 🔄 Translator")
    st.caption(
        "Convert imported `google_*` HCL into AWS / Azure equivalents. "
        "*(PUI-3 — coming soon)*"
    )
    st.markdown("#### 🔍 Detector")
    st.caption(
        "Compare cloud state vs Terraform state to find unmanaged drift. "
        "*(PUI-4 — coming soon)*"
    )
with col2:
    st.markdown("#### 🛡️ Policy")
    st.caption(
        "Scan resources against vendored Rego policies; report violations. "
        "*(PUI-5 — coming soon)*"
    )
    st.markdown("#### 📊 Dashboard")
    st.caption(
        "Cached snapshots of every engine's last run. "
        "*(PUI-2 — coming soon)*"
    )

st.markdown("---")
st.caption(
    "Phase 6 PUI-1 ships the Importer surface. The remaining engines "
    "land as PUI-2..PUI-5 once the per-page UX is settled."
)
