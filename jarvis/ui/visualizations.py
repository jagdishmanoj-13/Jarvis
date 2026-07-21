"""
ui/visualizations.py
======================

Generates the progress ring and the document "knowledge map" as inline
SVG strings, computed with pure Python + `math` — deliberately not
`matplotlib`/`networkx`/`plotly`, to stay consistent with this whole
project's Citrix-lightweight-dependency principle instead of adding a
heavyweight plotting stack just for two fairly simple visuals.

Design decisions
-----------------
- `progress_ring_svg()` replaces Streamlit's plain linear `st.progress`
  bar in the sidebar with an actual radial "progress ring" (spec:
  "progress rings while indexing"), drawn as a single SVG `<circle>` with
  a `stroke-dasharray` trick — a well-known, dependency-free way to render
  a ring gauge in pure SVG.
- `document_map_svg()` is a genuine radial layout, not a static mockup: it
  reads real watched-folder/document data from `MetadataStore`, computes
  node positions with basic trigonometry (folder at center, its documents
  arranged in a circle around it, multiple folders arranged around an
  outer ring), and sizes each document node by its actual chunk count —
  more content, bigger node. This satisfies the spec's "knowledge graph
  visualization" requirement with real data rather than a placeholder
  image, while staying inside the "no heavy dependency" constraint that
  matters for a Citrix deployment.
- Both functions return a raw `<svg>...</svg>` string meant to be rendered
  via `st.markdown(svg, unsafe_allow_html=True)` (Streamlit does not need
  `st.components.v1.html` for static, non-interactive SVG).
"""

from __future__ import annotations

import math
from typing import List

from database.metadata_store import MetadataStore


def progress_ring_svg(fraction: float, size: int = 64, label: str = "") -> str:
    fraction = max(0.0, min(1.0, fraction))
    radius = (size / 2) - 6
    circumference = 2 * math.pi * radius
    offset = circumference * (1 - fraction)
    center = size / 2
    percent_text = f"{int(fraction * 100)}%"

    return f"""
<svg width="{size}" height="{size}" viewBox="0 0 {size} {size}" style="display:block;">
  <circle cx="{center}" cy="{center}" r="{radius}" fill="none"
          stroke="rgba(99,179,237,0.15)" stroke-width="6" />
  <circle cx="{center}" cy="{center}" r="{radius}" fill="none"
          stroke="url(#jarvisRingGrad)" stroke-width="6" stroke-linecap="round"
          stroke-dasharray="{circumference:.2f}" stroke-dashoffset="{offset:.2f}"
          transform="rotate(-90 {center} {center})" style="transition: stroke-dashoffset 0.3s ease;" />
  <defs>
    <linearGradient id="jarvisRingGrad" x1="0%" y1="0%" x2="100%" y2="100%">
      <stop offset="0%" stop-color="#22d3ee" />
      <stop offset="100%" stop-color="#3b82f6" />
    </linearGradient>
  </defs>
  <text x="{center}" y="{center + 4}" text-anchor="middle" font-size="{size * 0.22:.0f}"
        font-weight="700" fill="#e8f0ff">{percent_text}</text>
</svg>
"""


def _node_color(element_count: int) -> str:
    if element_count > 15:
        return "#22d3ee"
    if element_count > 5:
        return "#3b82f6"
    return "#64748b"


def document_map_svg(store: MetadataStore, width: int = 640, height: int = 420, max_docs_per_folder: int = 8) -> str:
    folders = store.get_watched_folders()
    if not folders:
        return (
            f'<svg width="{width}" height="120" viewBox="0 0 {width} 120">'
            f'<text x="{width/2}" y="60" text-anchor="middle" fill="#8fa3c8" font-size="14">'
            f"No folders indexed yet — index a folder to see the document map.</text></svg>"
        )

    cx, cy = width / 2, height / 2
    outer_radius = min(width, height) / 2 - 40
    folder_count = len(folders)

    svg_parts: List[str] = []
    lines: List[str] = []
    nodes: List[str] = []

    for f_idx, folder in enumerate(folders):
        folder_angle = (2 * math.pi * f_idx / max(folder_count, 1)) - (math.pi / 2)
        f_radius = outer_radius if folder_count > 1 else 0
        fx = cx + f_radius * math.cos(folder_angle) * 0.55
        fy = cy + f_radius * math.sin(folder_angle) * 0.55

        # Center hub connector (only meaningful with >1 folder)
        if folder_count > 1:
            lines.append(f'<line x1="{cx}" y1="{cy}" x2="{fx:.1f}" y2="{fy:.1f}" '
                          f'stroke="rgba(99,179,237,0.25)" stroke-width="1.5" />')

        docs = store.list_documents(folder_path=folder["folder_path"])[:max_docs_per_folder]
        doc_count = max(len(docs), 1)

        for d_idx, doc in enumerate(docs):
            doc_angle = (2 * math.pi * d_idx / doc_count)
            doc_radius = 70
            dx = fx + doc_radius * math.cos(doc_angle)
            dy = fy + doc_radius * math.sin(doc_angle)

            chunk_count = len(store.get_chunks_for_document(doc.document_id))
            node_r = 5 + min(chunk_count, 20) * 0.6
            color = _node_color(chunk_count)

            lines.append(f'<line x1="{fx:.1f}" y1="{fy:.1f}" x2="{dx:.1f}" y2="{dy:.1f}" '
                          f'stroke="rgba(99,179,237,0.18)" stroke-width="1" />')
            label = doc.filename if len(doc.filename) <= 16 else doc.filename[:14] + "…"
            nodes.append(
                f'<circle cx="{dx:.1f}" cy="{dy:.1f}" r="{node_r:.1f}" fill="{color}" opacity="0.85">'
                f'<title>{doc.filename} ({chunk_count} chunks)</title></circle>'
                f'<text x="{dx:.1f}" y="{dy + node_r + 11:.1f}" text-anchor="middle" '
                f'font-size="9" fill="#8fa3c8">{label}</text>'
            )

        folder_r = 14
        nodes.append(
            f'<circle cx="{fx:.1f}" cy="{fy:.1f}" r="{folder_r}" fill="#0a1226" '
            f'stroke="#22d3ee" stroke-width="2"><title>{folder["display_name"]}</title></circle>'
            f'<text x="{fx:.1f}" y="{fy - folder_r - 6:.1f}" text-anchor="middle" '
            f'font-size="10" font-weight="700" fill="#e8f0ff">{folder["display_name"]}</text>'
        )

    svg_parts.append(f'<svg width="{width}" height="{height}" viewBox="0 0 {width} {height}">')
    svg_parts.extend(lines)
    svg_parts.extend(nodes)
    svg_parts.append("</svg>")
    return "\n".join(svg_parts)
