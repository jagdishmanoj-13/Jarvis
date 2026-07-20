"""
ui/app.py
=========

JARVIS — minimal functional Streamlit UI (MVP).

This is intentionally the SIMPLEST UI that is actually wired to the real
backend (config/database/cache/parser/indexing_service) built in Phases
1-2, so that running it produces a genuinely working tool rather than a
mockup. The full glassmorphic/premium UI (particle backgrounds, animated
cards, knowledge graph visualization, etc. from the original spec) is
still Phase 7 and will be layered on top of these same function calls —
none of this screen's logic will need to change, only its visual styling.

Design decisions
-----------------
- A single file for now (~150 lines): splitting into ui/components/*.py
  is deferred until Phase 7, when there's enough visual complexity to
  justify it. Splitting now would be premature structure around a UI
  that's about to be substantially redesigned.
- `st.session_state` holds the session_id (used as the conversation memory
  key) and last-active document/topic, mirroring the "conversation memory"
  spec requirement (last opened folder, last topic, follow-up context).
- The `MetadataStore` and `CacheManager` singletons are created once via
  `st.cache_resource`, not per-rerun — Streamlit reruns the whole script
  on every interaction, so without this we'd reopen the SQLite connection
  pool and re-scan the cache directory structure on every keystroke.
- Folder indexing runs synchronously with a progress bar for this MVP.
  Phase 3's background threading will make this non-blocking; the
  `progress_callback` hook in `index_folder()` already anticipates that.
"""

from __future__ import annotations

import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import streamlit as st

from cache.cache_manager import get_cache
from config.settings import get_settings
from core.background_indexer import get_job_registry
from core.indexing_service import search_chunks
from database.metadata_store import MetadataStore
from models.document import ConversationTurn

st.set_page_config(page_title="JARVIS — Engineering Knowledge Assistant", page_icon="🛠️", layout="wide")


@st.cache_resource
def get_store() -> MetadataStore:
    return MetadataStore()


@st.cache_resource
def get_cache_manager():
    return get_cache()


def init_session_state():
    if "session_id" not in st.session_state:
        st.session_state.session_id = str(uuid.uuid4())
    if "chat_history" not in st.session_state:
        st.session_state.chat_history = []  # list of (role, text, citation_str|None)
    if "last_document_id" not in st.session_state:
        st.session_state.last_document_id = None


def render_sidebar(store: MetadataStore, cache):
    settings = get_settings()
    with st.sidebar:
        st.markdown("## 🛠️ JARVIS")
        st.caption(settings.app_tagline)
        st.divider()

        st.markdown("### Index a folder")
        folder_input = st.text_input("Folder path", placeholder=r"C:\Engineering\Manuals")
        if st.button("Scan & Index", use_container_width=True, type="primary"):
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
                    label = f"{snap['done']}/{snap['total']} — {snap['current_file']}" if snap["total"] else "Scanning..."
                    st.progress(snap["fraction"], text=label)
                    st.caption("Indexing runs in the background — you can keep using the chat below while this finishes.")
                    time.sleep(1.0)
                    st.rerun()
                elif snap["error"]:
                    st.error(f"Indexing failed: {snap['error']}")
                    del st.session_state["active_index_folder"]
                elif snap["stats"]:
                    s = snap["stats"]
                    st.success(
                        f"Indexed {s.indexed} | Unchanged {s.skipped_unchanged} | "
                        f"Archives expanded {s.archives_expanded} | "
                        f"Unsupported {s.unsupported} | Failed {s.failed}"
                    )
                    if s.failed_files:
                        with st.expander(f"⚠️ {len(s.failed_files)} file(s) had issues"):
                            for f in s.failed_files:
                                st.text(f)
                    store.set_app_state("last_opened_folder", active_folder)
                    del st.session_state["active_index_folder"]

        st.divider()
        st.markdown("### Watched folders")
        folders = store.get_watched_folders()
        if not folders:
            st.caption("No folders indexed yet.")
        for f in folders:
            doc_count = len(store.list_documents(folder_path=f["folder_path"]))
            st.markdown(f"**{f['display_name']}**  \n`{f['folder_path']}`  \n{doc_count} document(s)")

        st.divider()
        st.markdown("### System")
        all_docs = store.list_documents()
        st.metric("Documents indexed", len(all_docs))
        cache_stats = cache.stats()
        total_cache_mb = round(sum(s["size_mb"] for s in cache_stats.values()), 2)
        st.metric("Cache size", f"{total_cache_mb} MB")
        st.caption(f"Knowledge base: `{settings.db_path}`")


def render_chat(store: MetadataStore):
    st.markdown("### Ask JARVIS about your engineering documents")

    for role, text, citation in st.session_state.chat_history:
        with st.chat_message(role):
            st.write(text)
            if citation:
                st.caption(f"📄 {citation}")

    question = st.chat_input("Ask a question about your indexed documents...")
    if not question:
        return

    st.session_state.chat_history.append(("user", question, None))
    store.add_conversation_turn(ConversationTurn(
        session_id=st.session_state.session_id, role="user", content=question,
        active_document_id=st.session_state.last_document_id,
    ))

    with st.chat_message("user"):
        st.write(question)

    hits = search_chunks(store, question, limit=5)

    with st.chat_message("assistant"):
        if not hits:
            answer = (
                "I couldn't find a matching passage in the indexed documents for that question. "
                "Try different keywords, or index the folder that contains the relevant file."
            )
            st.write(answer)
            st.session_state.chat_history.append(("assistant", answer, None))
        else:
            top = hits[0]
            citation_loc = f"p.{top['page_number']}" if top["page_number"] else (top["section_path"] or "")
            citation = f"{top['filename']}" + (f" ({citation_loc})" if citation_loc else "")
            answer = top["text"]
            st.write(answer)
            st.caption(f"📄 {citation}")

            if len(hits) > 1:
                with st.expander(f"{len(hits) - 1} more relevant passage(s)"):
                    for h in hits[1:]:
                        loc = f"p.{h['page_number']}" if h["page_number"] else (h["section_path"] or "")
                        st.markdown(f"**{h['filename']}**" + (f" ({loc})" if loc else ""))
                        st.text(h["text"][:400])
                        st.divider()

            st.session_state.chat_history.append(("assistant", answer, citation))
            st.session_state.last_document_id = top["document_id"]

            store.add_conversation_turn(ConversationTurn(
                session_id=st.session_state.session_id, role="assistant", content=answer,
                active_topic=question, active_document_id=top["document_id"],
            ))

    st.caption(
        "⚠️ MVP note: answers are the single best-matching passage from keyword search, shown verbatim "
        "with its source citation — there is no language-generation/summarization model in the loop yet "
        "(by design; see project README). Phase 4 will add ranked hybrid search; Phase 6 will add the "
        "pluggable LanguageGenerationEngine."
    )


def main():
    init_session_state()
    store = get_store()
    cache = get_cache_manager()

    render_sidebar(store, cache)
    render_chat(store)


if __name__ == "__main__":
    main()
