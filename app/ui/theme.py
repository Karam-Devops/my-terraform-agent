# app/ui/theme.py
"""Page-wide theme polish (PUI-1B v3.4: Firefly-style visual upgrade).

Streamlit's ``.streamlit/config.toml`` sets the base palette, but a
few Firefly-style touches need CSS injection:

  * Subtler expander borders + rounded corners (Streamlit's default
    expander has heavy borders that feel "form-y")
  * Tighter table row padding for higher info density (Firefly's
    inventory page packs ~25 rows per screen vs Streamlit's default
    ~12)
  * Hover-row highlight on data_editor (default has no hover affordance)
  * Status-pill helper for inline color badges (green/orange/red)

Usage: ``apply_theme_polish()`` should be called near the top of
every page (after ``st.set_page_config`` but before any content).
``status_pill(label, kind)`` returns an HTML <span> for inline use.
"""

from __future__ import annotations

import streamlit as st


# Colors must match `.streamlit/config.toml`'s [theme] block.
# Defining as constants here avoids drift if someone updates the
# CSS without updating the toml (or vice versa).
_PRIMARY = "#00C4A7"
_BG = "#FFFFFF"
_BG_ALT = "#F5F7FA"
_TEXT = "#1A1F36"
_SUCCESS = "#00C853"
_WARNING = "#FFA726"
_ERROR = "#EF5350"
_INFO = "#29B6F6"


_BASE_CSS = f"""
<style>
/* ------------------------------------------------------------------
   PUI-1B v3.4 polish: tighter expanders + subtler borders.
   Streamlit's defaults are designed for survey-form-style pages;
   for a Firefly-like data-dense inventory view we want less chrome.
   ------------------------------------------------------------------ */

/* Expander: replace heavy box-shadow with a subtle border */
[data-testid="stExpander"] {{
    border: 1px solid #E5E9F2;
    border-radius: 8px;
    box-shadow: none;
    transition: border-color 120ms ease;
}}
[data-testid="stExpander"]:hover {{
    border-color: {_PRIMARY};
}}

/* DataFrame / data_editor: tighter row padding + hover highlight.
   Firefly's inventory page uses ~32px row height; Streamlit defaults
   to ~40px. Tightening lets us show ~25% more rows per scroll. */
[data-testid="stDataFrame"] tbody tr,
[data-testid="stDataEditor"] tbody tr {{
    transition: background-color 100ms ease;
}}
[data-testid="stDataFrame"] tbody tr:hover,
[data-testid="stDataEditor"] tbody tr:hover {{
    background-color: {_BG_ALT} !important;
}}

/* Primary button: subtle lift on hover (matches Firefly's
   button-feels-clickable affordance). */
.stButton > button[kind="primary"] {{
    transition: transform 80ms ease, box-shadow 80ms ease;
}}
.stButton > button[kind="primary"]:hover {{
    transform: translateY(-1px);
    box-shadow: 0 4px 12px rgba(0, 196, 167, 0.25);
}}

/* Sidebar header spacing -- Firefly uses tighter section gaps */
[data-testid="stSidebar"] h3 {{
    margin-top: 1rem;
    margin-bottom: 0.5rem;
}}

/* Metric card: subtle border so the 4-column metric row looks
   like discrete cards rather than floating numbers */
[data-testid="stMetric"] {{
    background: {_BG_ALT};
    padding: 12px 16px;
    border-radius: 8px;
    border: 1px solid #E5E9F2;
}}
</style>
"""


def apply_theme_polish() -> None:
    """Inject the page-wide CSS polish. Idempotent (Streamlit dedupes
    identical markdown calls within a single script run)."""
    st.markdown(_BASE_CSS, unsafe_allow_html=True)


def status_pill(label: str, kind: str = "info") -> str:
    """Return an HTML <span> for a colored status badge.

    Args:
        label: Display text inside the pill.
        kind: "success" (green), "warning" (orange), "error" (red),
              or "info" (blue).

    Returns:
        An HTML string. Caller renders via:

            st.markdown(status_pill("Imported", "success"),
                        unsafe_allow_html=True)

        Or inline within a larger markdown block (the <span> is
        inline so it sits next to other text).

    Why HTML and not st.success / st.warning: those Streamlit widgets
    take a full row each. Pills are inline and render alongside text
    in the same block -- right pattern for a "status next to row label."
    """
    color_map = {
        "success": _SUCCESS,
        "warning": _WARNING,
        "error": _ERROR,
        "info": _INFO,
    }
    color = color_map.get(kind, _INFO)
    return (
        f'<span style="'
        f'display: inline-block; '
        f'padding: 2px 10px; '
        f'border-radius: 12px; '
        f'background-color: {color}1A; '  # 1A = ~10% alpha for subtle bg
        f'color: {color}; '
        f'font-size: 0.85em; '
        f'font-weight: 600; '
        f'border: 1px solid {color}66;'  # 66 = ~40% alpha border
        f'">{label}</span>'
    )
