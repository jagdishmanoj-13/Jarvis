"""
ui/theme.py
============

The visual design system for the premium UI: dark glassmorphism, blue/cyan
gradients, animated accents — matching the "Iron Man / Apple Vision Pro /
Tesla / Copilot" brief from the original spec.

Design decisions
-----------------
- Implemented as one injected `<style>` block (`st.markdown(..., unsafe_allow_html=True)`),
  not a Streamlit theme config file — Streamlit's native theming (`.streamlit/config.toml`)
  can't express glassmorphism (backdrop-filter blur), gradient buttons, or
  keyframe animations, so CSS injection is the only way to hit the actual
  brief within Streamlit's constraints.
- CSS custom properties (`--jarvis-*` variables) drive both the dark and
  light palettes from ONE stylesheet — `theme_css(dark=True/False)` just
  swaps the variable block, so every component styled against the
  variables (not hardcoded colors) automatically reskins for the theme
  toggle without duplicating any rules.
- The particle background is a handful of small `<div>`s with staggered
  CSS keyframe animations (`@keyframes float`), not a JS particle engine —
  this keeps it dependency-free (no canvas/JS library to vet or fail to
  load on a locked-down Citrix browser policy) while still being a real
  animated background, not a static image.
- Kept intentionally light (a few KB of CSS): heavy custom fonts or
  external CSS frameworks would mean a network fetch, which breaks on an
  offline Citrix machine — everything here is inline, system-font-based,
  and renders identically with zero internet access.
"""

from __future__ import annotations

_DARK_VARS = """
    --jarvis-bg-primary: #060b18;
    --jarvis-bg-secondary: #0a1226;
    --jarvis-glass: rgba(18, 28, 54, 0.55);
    --jarvis-glass-border: rgba(99, 179, 237, 0.18);
    --jarvis-text-primary: #e8f0ff;
    --jarvis-text-secondary: #8fa3c8;
    --jarvis-accent-cyan: #22d3ee;
    --jarvis-accent-blue: #3b82f6;
    --jarvis-accent-glow: rgba(34, 211, 238, 0.35);
    --jarvis-success: #34d399;
    --jarvis-warning: #fbbf24;
    --jarvis-danger: #f87171;
"""

_LIGHT_VARS = """
    --jarvis-bg-primary: #eef3fb;
    --jarvis-bg-secondary: #f7faff;
    --jarvis-glass: rgba(255, 255, 255, 0.65);
    --jarvis-glass-border: rgba(59, 130, 246, 0.18);
    --jarvis-text-primary: #0b1730;
    --jarvis-text-secondary: #445573;
    --jarvis-accent-cyan: #0891b2;
    --jarvis-accent-blue: #2563eb;
    --jarvis-accent-glow: rgba(37, 99, 235, 0.20);
    --jarvis-success: #059669;
    --jarvis-warning: #b45309;
    --jarvis-danger: #dc2626;
"""


def theme_css(dark: bool = True) -> str:
    variables = _DARK_VARS if dark else _LIGHT_VARS
    return f"""
<style>
:root {{
{variables}
}}

.stApp {{
    background:
        radial-gradient(ellipse 80% 50% at 20% -10%, var(--jarvis-accent-glow), transparent),
        radial-gradient(ellipse 60% 40% at 90% 10%, var(--jarvis-accent-glow), transparent),
        var(--jarvis-bg-primary);
    color: var(--jarvis-text-primary);
}}

/* ---- Particle background: small glowing dots drifting upward ---- */
.jarvis-particles {{
    position: fixed; inset: 0; overflow: hidden; pointer-events: none; z-index: 0;
}}
.jarvis-particle {{
    position: absolute; bottom: -10px; border-radius: 50%;
    background: var(--jarvis-accent-cyan); opacity: 0.35;
    animation: jarvis-float linear infinite;
    box-shadow: 0 0 8px var(--jarvis-accent-cyan);
}}
@keyframes jarvis-float {{
    0%   {{ transform: translateY(0) translateX(0); opacity: 0; }}
    10%  {{ opacity: 0.5; }}
    100% {{ transform: translateY(-110vh) translateX(20px); opacity: 0; }}
}}

/* ---- Header banner ---- */
.jarvis-header {{
    display: flex; align-items: center; gap: 14px; padding: 18px 22px; margin-bottom: 18px;
    background: var(--jarvis-glass); border: 1px solid var(--jarvis-glass-border);
    border-radius: 18px; backdrop-filter: blur(14px); -webkit-backdrop-filter: blur(14px);
    box-shadow: 0 8px 32px rgba(0,0,0,0.25);
}}
.jarvis-header-title {{
    font-size: 1.6rem; font-weight: 700; margin: 0;
    background: linear-gradient(90deg, var(--jarvis-accent-cyan), var(--jarvis-accent-blue));
    -webkit-background-clip: text; -webkit-text-fill-color: transparent; background-clip: text;
}}
.jarvis-header-sub {{ color: var(--jarvis-text-secondary); font-size: 0.85rem; margin: 0; }}
.jarvis-status-dot {{
    width: 10px; height: 10px; border-radius: 50%; background: var(--jarvis-success);
    box-shadow: 0 0 10px var(--jarvis-success); animation: jarvis-pulse 2s ease-in-out infinite;
}}
@keyframes jarvis-pulse {{
    0%, 100% {{ opacity: 1; transform: scale(1); }}
    50% {{ opacity: 0.5; transform: scale(0.85); }}
}}

/* ---- Glass cards (metrics, watched folders, etc.) ---- */
.jarvis-card {{
    background: var(--jarvis-glass); border: 1px solid var(--jarvis-glass-border);
    border-radius: 14px; padding: 14px 16px; margin-bottom: 10px;
    backdrop-filter: blur(10px); -webkit-backdrop-filter: blur(10px);
    transition: transform 0.15s ease, box-shadow 0.15s ease;
}}
.jarvis-card:hover {{ transform: translateY(-2px); box-shadow: 0 6px 20px rgba(34,211,238,0.15); }}
.jarvis-card-title {{ font-weight: 600; font-size: 0.92rem; color: var(--jarvis-text-primary); margin-bottom: 2px; }}
.jarvis-card-sub {{ font-size: 0.78rem; color: var(--jarvis-text-secondary); }}

/* ---- Buttons: gradient, glowing on hover ---- */
.stButton > button {{
    background: linear-gradient(135deg, var(--jarvis-accent-blue), var(--jarvis-accent-cyan));
    color: white; border: none; border-radius: 10px; font-weight: 600;
    transition: box-shadow 0.2s ease, transform 0.1s ease;
}}
.stButton > button:hover {{
    box-shadow: 0 0 18px var(--jarvis-accent-glow); transform: translateY(-1px);
}}

/* ---- Chat bubbles ---- */
[data-testid="stChatMessage"] {{
    background: var(--jarvis-glass) !important; border: 1px solid var(--jarvis-glass-border) !important;
    border-radius: 16px !important; backdrop-filter: blur(10px);
}}

/* ---- Badges (intent / match-reason tags) ---- */
.jarvis-badge {{
    display: inline-block; padding: 2px 9px; border-radius: 999px; font-size: 0.72rem;
    font-weight: 600; margin-right: 6px; background: rgba(34,211,238,0.15);
    color: var(--jarvis-accent-cyan); border: 1px solid rgba(34,211,238,0.3);
}}
.jarvis-badge-warning {{
    background: rgba(251,191,36,0.15); color: var(--jarvis-warning); border-color: rgba(251,191,36,0.3);
}}

/* ---- Progress ring wrapper ---- */
.jarvis-ring-wrap {{ display: flex; align-items: center; gap: 12px; }}

/* ---- Section headers ---- */
.jarvis-section-label {{
    font-size: 0.72rem; letter-spacing: 0.08em; text-transform: uppercase;
    color: var(--jarvis-text-secondary); margin: 14px 0 6px 0; font-weight: 700;
}}

section[data-testid="stSidebar"] {{
    background: var(--jarvis-bg-secondary); border-right: 1px solid var(--jarvis-glass-border);
}}
</style>
"""


def particles_html(count: int = 18) -> str:
    """Generates the floating particle background as plain HTML/CSS —
    positions/sizes/durations computed with pure Python math, no JS.
    """
    import random
    random.seed(42)  # stable look across reruns rather than re-randomizing every interaction
    divs = []
    for i in range(count):
        left = random.uniform(0, 100)
        size = random.uniform(2, 5)
        duration = random.uniform(14, 30)
        delay = random.uniform(0, 20)
        divs.append(
            f'<div class="jarvis-particle" style="left:{left:.1f}%; width:{size:.1f}px; '
            f'height:{size:.1f}px; animation-duration:{duration:.1f}s; animation-delay:-{delay:.1f}s;"></div>'
        )
    return f'<div class="jarvis-particles">{"".join(divs)}</div>'
