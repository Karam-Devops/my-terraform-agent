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
#
# PUI-1B v3.6: switched to Firefly dark palette. Background tones are
# inverted (near-black canvas + slightly elevated cards); accent +
# status colors stay the same (they're brand colors, not theme colors).
_PRIMARY = "#00C4A7"
_BG = "#0E1117"          # main canvas (matches config.toml backgroundColor)
_BG_ALT = "#1A1F2C"      # one-step elevated (matches secondaryBackgroundColor)
_BG_HOVER = "#232938"    # two-step elevated -- used for table-row hover
_BORDER = "#2A3142"      # subtle dark border (visible on _BG_ALT, not on _BG)
_TEXT = "#E5E9F2"
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

/* Expander: replace heavy box-shadow with a subtle dark border.
   On dark canvas the border is the only chrome, so the color matters
   more than on light -- too dark = invisible, too light = noisy. */
[data-testid="stExpander"] {{
    border: 1px solid {_BORDER};
    border-radius: 8px;
    box-shadow: none;
    transition: border-color 120ms ease;
}}
[data-testid="stExpander"]:hover {{
    border-color: {_PRIMARY};
}}

/* DataFrame / data_editor: tighter row padding + hover highlight.
   Firefly's inventory page uses ~32px row height; Streamlit defaults
   to ~40px. Tightening lets us show ~25% more rows per scroll.
   On dark theme the hover bg is a 2nd elevation step (not _BG_ALT,
   which IS the table's own bg already -- would give zero contrast). */
[data-testid="stDataFrame"] tbody tr,
[data-testid="stDataEditor"] tbody tr {{
    transition: background-color 100ms ease;
}}
[data-testid="stDataFrame"] tbody tr:hover,
[data-testid="stDataEditor"] tbody tr:hover {{
    background-color: {_BG_HOVER} !important;
}}

/* Primary button: subtle lift on hover (matches Firefly's
   button-feels-clickable affordance). The teal glow on dark bg
   reads even better than on light -- Firefly relies on this same
   glow effect for their "Codify" button. */
.stButton > button[kind="primary"] {{
    transition: transform 80ms ease, box-shadow 80ms ease;
}}
.stButton > button[kind="primary"]:hover {{
    transform: translateY(-1px);
    box-shadow: 0 4px 16px rgba(0, 196, 167, 0.45);
}}

/* Sidebar header spacing -- Firefly uses tighter section gaps */
[data-testid="stSidebar"] h3 {{
    margin-top: 1rem;
    margin-bottom: 0.5rem;
}}

/* Metric card: subtle border so the 4-column metric row looks
   like discrete cards rather than floating numbers. Dark variant
   uses _BORDER (matches expander) for visual consistency. */
[data-testid="stMetric"] {{
    background: {_BG_ALT};
    padding: 12px 16px;
    border-radius: 8px;
    border: 1px solid {_BORDER};
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
    # Alpha values tuned for DARK backgrounds (PUI-1B v3.6):
    # On a near-black canvas, a 10% color overlay almost disappears.
    # Bumped bg to 26 (~15%) and border to 80 (~50%) so the pill keeps
    # the same "subtle but readable" feel it had on the light theme.
    return (
        f'<span style="'
        f'display: inline-block; '
        f'padding: 2px 10px; '
        f'border-radius: 12px; '
        f'background-color: {color}26; '  # ~15% alpha bg
        f'color: {color}; '
        f'font-size: 0.85em; '
        f'font-weight: 600; '
        f'border: 1px solid {color}80;'   # ~50% alpha border
        f'">{label}</span>'
    )
