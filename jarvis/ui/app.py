"""
ui/app.py
=========

JARVIS — full premium UI (Phase 7).

Everything here is styling and presentation layered on top of the exact
same backend calls proven in Phases 1-6 (`MetadataStore`, `index_folder`
via the background job registry, `answer_question`) — none of the
underlying logic changed for this visual pass, only how it's presented.

Design decisions
-----------------
- `ui/theme.py` injects the glassmorphic dark/light CSS; `ui/visualizations.py`
  generates the progress ring and document map as real SVG from real data.
  Neither adds a runtime dependency beyond what Phases 1-6 already needed.
- Favorites/bookmarks and the dark/light theme choice are persisted via
  `MetadataStore.set_app_state`/`get_app_state` (already built in Phase 1)
  as a JSON blob, so they survive an app restart — consistent with the
  "remembers... across restarts" memory requirement applied to UI
  preferences, not just conversation history.
- The typing animation reveals the composed answer progressively via a
  single `st.empty()` placeholder updated in a tight loop. Since answers
  are already fully composed in milliseconds (no LLM streaming to wait
  on), this is purely a presentation effect, revealed word-by-word rather
  than character-by-character to stay fast even on long list/comparison
  answers.
"""

from __future__ import annotations

import json
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import streamlit as st

from cache.cache_manager import get_cache
from config.settings import get_settings
from core.background_indexer import get_job_registry
from core.qa_service import answer_question
from database.metadata_store import MetadataStore
from ui.theme import particles_html, theme_css
from ui.visualizations import document_map_svg, progress_ring_svg

st.set_page_config(page_title="JARVIS — Engineering Knowledge Assistant", page_icon="🛠️", layout="wide")


@st.cache_resource
def get_store() -> MetadataStore:
    return MetadataStore()


@st.cache_resource
def get_cache_manager():
    return get_cache()


def _get_favorites(store: MetadataStore) -> list:
    raw = store.get_app_state("favorites")
    if not raw:
        return []
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return []


def _add_favorite(store: MetadataStore, question: str, answer: str) -> None:
    favorites = _get_favorites(store)
    favorites.insert(0, {"question": question, "answer": answer[:300]})
    store.set_app_state("favorites", json.dumps(favorites[:20]))


def init_session_state(store: MetadataStore):
    if "session_id" not in st.session_state:
        st.session_state.session_id = str(uuid.uuid4())
    if "chat_history" not in st.session_state:
        st.session_state.chat_history = []  # list of dicts: role, text, citations, intent, low_conf, used_math
    if "dark_mode" not in st.session_state:
        stored = store.get_app_state("theme_dark")
        st.session_state.dark_mode = (stored != "false")  # default dark


def render_header():
    st.markdown(
        """
        <div class="jarvis-header">
            <div class="jarvis-status-dot"></div>
            <div>
                <p class="jarvis-header-title">JARVIS</p>
                <p class="jarvis-header-sub">Engineering Knowledge Assistant — fully local, zero LLM</p>
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def render_sidebar(store: MetadataStore, cache):
    settings = get_settings()
    with st.sidebar:
        col1, col2 = st.columns([3, 1])
        with col1:
            st.markdown("## 🛠️ JARVIS")
        with col2:
            if st.button("🌓", help="Toggle dark/light theme", use_container_width=True):
                st.session_state.dark_mode = not st.session_state.dark_mode
                store.set_app_state("theme_dark", "true" if st.session_state.dark_mode else "false")
                st.rerun()
        st.caption(settings.app_tagline)

        st.markdown('<p class="jarvis-section-label">Index a folder</p>', unsafe_allow_html=True)
        folder_input = st.text_input("Folder path", placeholder=r"C:\Engineering\Manuals", label_visibility="collapsed")
        if st.button("⚡ Scan & Index", use_container_width=True, type="primary"):
            path = Path(folder_input).expanduser()
            if not path.exists() or not path.is_dir():
                st.error(f"Folder not found: {path}")
            else:
                st.session_state.active_index_folder = str(path)
                get_job_registry().start(store, path)
                st.rerun()

        active_folder = st.session_state.get("active_index_folder")
        if active_folder:
            job = get_job_registry().get(Path(active_folder))
            if job:
                snap = job.snapshot()
                if snap["is_running"]:
                    ring_col, label_col = st.columns([1, 2])
                    with ring_col:
                        st.markdown(progress_ring_svg(snap["fraction"], size=64), unsafe_allow_html=True)
                    with label_col:
                        st.caption(f"{snap['done']}/{snap['total']}" if snap["total"] else "Scanning...")
                        st.caption(snap["current_file"][:28])
                    st.caption("Running in the background — chat still works while this finishes.")
                    time.sleep(1.0)
                    st.rerun()
                elif snap["error"]:
                    st.error(f"Indexing failed: {snap['error']}")
                    del st.session_state["active_index_folder"]
                elif snap["stats"]:
                    s = snap["stats"]
                    st.success(
                        f"Indexed {s.indexed} · Unchanged {s.skipped_unchanged} · "
                        f"Archives {s.archives_expanded} · Unsupported {s.unsupported} · Failed {s.failed}"
                    )
                    if s.failed_files:
                        with st.expander(f"⚠️ {len(s.failed_files)} file(s) had issues"):
                            for f in s.failed_files:
                                st.text(f)
                    store.set_app_state("last_opened_folder", active_folder)
                    del st.session_state["active_index_folder"]

        st.markdown('<p class="jarvis-section-label">Watched folders</p>', unsafe_allow_html=True)
        folders = store.get_watched_folders()
        if not folders:
            st.caption("No folders indexed yet.")
        for f in folders:
            doc_count = len(store.list_documents(folder_path=f["folder_path"]))
            st.markdown(
                f'<div class="jarvis-card"><div class="jarvis-card-title">📁 {f["display_name"]}</div>'
                f'<div class="jarvis-card-sub">{f["folder_path"]}</div>'
                f'<div class="jarvis-card-sub">{doc_count} document(s) · last scanned {f["last_scanned_at"] or "never"}</div></div>',
                unsafe_allow_html=True,
            )

        st.markdown('<p class="jarvis-section-label">System</p>', unsafe_allow_html=True)
        all_docs = store.list_documents()
        cache_stats = cache.stats()
        total_cache_mb = round(sum(s["size_mb"] for s in cache_stats.values()), 2)

        m1, m2 = st.columns(2)
        m1.metric("Documents", len(all_docs))
        m2.metric("Cache", f"{total_cache_mb} MB")

        try:
            import psutil
            cpu = psutil.cpu_percent(interval=0.1)
            mem = psutil.virtual_memory().percent
            m3, m4 = st.columns(2)
            m3.metric("CPU", f"{cpu:.0f}%")
            m4.metric("RAM", f"{mem:.0f}%")
        except ImportError:
            pass

        st.caption(f"Knowledge base: `{settings.db_path}`")

        favorites = _get_favorites(store)
        if favorites:
            st.markdown('<p class="jarvis-section-label">⭐ Favorites</p>', unsafe_allow_html=True)
            for fav in favorites[:5]:
                with st.expander(fav["question"][:50]):
                    st.caption(fav["answer"])


def _type_out(placeholder, text: str, citation_html: str = "", words_per_tick: int = 3, delay: float = 0.012):
    """Reveals `text` progressively (word-by-word, not char-by-char, to
    stay fast on long answers) in the given st.empty() placeholder --
    the spec's "animated typing / streaming answers" requirement, applied
    to an already-fully-composed (non-LLM) answer purely for presentation.
    """
    words = text.split(" ")
    shown = []
    for i in range(0, len(words), words_per_tick):
        shown.extend(words[i:i + words_per_tick])
        placeholder.markdown(" ".join(shown) + " ▌")
        time.sleep(delay)
    placeholder.markdown(text + citation_html)


def render_chat(store: MetadataStore):
    st.markdown("#### 💬 Ask about your indexed documents")

    for turn in st.session_state.chat_history:
        with st.chat_message(turn["role"]):
            st.write(turn["text"])
            if turn.get("citations"):
                st.caption("📄 " + " · ".join(turn["citations"][:5]))
            if turn.get("intent"):
                badge = f'<span class="jarvis-badge">🧭 {turn["intent"]}</span>'
                if turn.get("used_math"):
                    badge += '<span class="jarvis-badge">🧮 math engine</span>'
                if turn.get("low_conf"):
                    badge += '<span class="jarvis-badge jarvis-badge-warning">⚠️ OCR source</span>'
                st.markdown(badge, unsafe_allow_html=True)

    question = st.chat_input("Ask a question about your indexed documents...")
    if not question:
        return

    st.session_state.chat_history.append({"role": "user", "text": question})
    with st.chat_message("user"):
        st.write(question)

    with st.chat_message("assistant"):
        response = answer_question(store, st.session_state.session_id, question)
        answer = response.answer

        placeholder = st.empty()
        _type_out(placeholder, answer.text)

        citation_strs = [c.render() for c in answer.citations]
        if citation_strs:
            st.caption("📄 " + " · ".join(citation_strs[:5]))
        if answer.low_confidence:
            st.caption("⚠️ Source includes OCR'd content — double-check against the original if precision matters.")

        badge = f'<span class="jarvis-badge">🧭 {response.intent.value}</span>'
        if answer.used_math_engine:
            badge += '<span class="jarvis-badge">🧮 math engine</span>'
        st.markdown(badge, unsafe_allow_html=True)

        if st.button("⭐ Save as favorite", key=f"fav_{len(st.session_state.chat_history)}"):
            _add_favorite(store, question, answer.text)
            st.toast("Saved to favorites")

        st.session_state.chat_history.append({
            "role": "assistant", "text": answer.text, "citations": citation_strs,
            "intent": response.intent.value, "used_math": answer.used_math_engine,
            "low_conf": answer.low_confidence,
        })

    st.caption(
        "ℹ️ Answers are composed by JARVIS's own rule-based grammar/communication/math engine "
        "(no LLM) from extracted passages in your indexed documents — never invented text."
    )


def render_document_map(store: MetadataStore):
    with st.expander("🗺️ Document Map", expanded=False):
        st.caption("A live map of indexed folders and documents, sized by how much content each contains.")
        svg = document_map_svg(store)
        st.markdown(svg, unsafe_allow_html=True)


def main():
    store = get_store()
    cache = get_cache_manager()
    init_session_state(store)

    st.markdown(theme_css(dark=st.session_state.dark_mode), unsafe_allow_html=True)
    st.markdown(particles_html(), unsafe_allow_html=True)

    render_header()
    render_sidebar(store, cache)
    render_document_map(store)
    render_chat(store)


if __name__ == "__main__":
    main()
