"""
demo/run_demo.py
=================

A runnable, end-to-end demonstration of everything built so far:
  1. Register a folder as "watched" (remembered across restarts).
  2. Recursively scan it, parse every supported file, chunk the content,
     and store documents + chunks in the SQLite knowledge base.
  3. Run a few example questions against the FTS5 keyword index directly
     (a lightweight stand-in for the full hybrid Retrieval layer, which is
     Phase 4 and not built yet) to show that the right chunk, with the
     right page/section/table citation, comes back for a real question.
  4. Record the Q&A as conversation memory and show a context-aware
     follow-up question working off "last document" / "last topic".

This file is NOT the final retrieval engine — it's a demo harness proving
Phases 1-2 (foundations + parsing) produce a genuinely searchable,
citable knowledge base, ahead of Phase 4 building the real ranked hybrid
search on top of the same FTS5 table.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.settings import get_settings
from database.metadata_store import MetadataStore
from cache.cache_manager import get_cache
from models.document import DocumentMetadata, IndexStatus, ConversationTurn
from parser.registry import get_parser_for
from parser.base_parser import ParserError
from parser.chunker import TextChunker
from utils.hashing import compute_file_hash, quick_fingerprint
from utils.logger import get_logger
from datetime import datetime, timezone

logger = get_logger("demo")


def index_folder(store: MetadataStore, folder: Path) -> dict:
    """Recursive scan + incremental parse, mirroring what the Phase-3
    indexer will automate on a background thread.
    """
    chunker = TextChunker()
    stats = {"scanned": 0, "indexed": 0, "skipped_unchanged": 0, "failed": 0, "unsupported": 0}
    seen_paths = []

    store.add_watched_folder(str(folder), display_name=folder.name)

    all_files = [p for p in folder.rglob("*") if p.is_file()]
    for path in all_files:
        stats["scanned"] += 1
        seen_paths.append(str(path.resolve()))

        existing = store.get_document_by_path(str(path.resolve()))
        content_hash = compute_file_hash(path)

        if existing and existing.content_hash == content_hash and existing.index_status == IndexStatus.INDEXED:
            stats["skipped_unchanged"] += 1
            continue  # incremental indexing: unchanged file, don't re-parse

        parser = get_parser_for(path)
        if parser is None:
            stats["unsupported"] += 1
            continue

        doc_meta = DocumentMetadata(
            filename=path.name, extension=path.suffix, path=str(path.resolve()),
            created_date=datetime.fromtimestamp(path.stat().st_ctime, tz=timezone.utc),
            modified_date=datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc),
            content_hash=content_hash, size_bytes=path.stat().st_size,
        )

        try:
            parsed = parser.parse(path)
            doc_meta.title = parsed.title
            doc_meta.author = parsed.author
            doc_meta.page_count = parsed.page_count
            doc_meta.index_status = IndexStatus.INDEXED
            doc_meta.indexed_at = datetime.now(timezone.utc)
            store.upsert_document(doc_meta, folder_path=str(folder.resolve()))

            stored_doc = store.get_document_by_path(str(path.resolve()))
            chunks = chunker.chunk(stored_doc.document_id, parsed)
            store.replace_chunks_for_document(stored_doc.document_id, chunks)
            stats["indexed"] += 1
        except ParserError as exc:
            doc_meta.index_status = IndexStatus.FAILED
            doc_meta.last_error = str(exc)
            store.upsert_document(doc_meta, folder_path=str(folder.resolve()))
            stats["failed"] += 1
            logger.warning("Failed to index %s: %s", path, exc)

    store.mark_documents_deleted(seen_paths, str(folder.resolve()))
    store.touch_watched_folder(str(folder))
    return stats


def search_chunks(store: MetadataStore, query: str, limit: int = 3):
    """Direct FTS5 query — a preview of Phase 4's keyword search, joined
    back to document metadata so we can show a real citation.
    """
    with store._connect() as conn:
        rows = conn.execute(
            """
            SELECT d.filename, d.path, c.text, c.element_type, c.page_number, c.section_path,
                   bm25(chunks_fts) AS rank
            FROM chunks_fts
            JOIN chunks c ON c.rowid = chunks_fts.rowid
            JOIN documents d ON d.document_id = c.document_id
            WHERE chunks_fts MATCH ?
            ORDER BY rank
            LIMIT ?
            """,
            (query, limit),
        ).fetchall()
        return [dict(r) for r in rows]


def main():
    settings = get_settings()
    print(f"JARVIS home directory: {settings.home_dir}\n")

    store = MetadataStore()
    cache = get_cache()

    folder = Path(__file__).resolve().parent.parent.parent / "sample_docs"
    print(f"=== Indexing folder: {folder} ===")
    t0 = time.time()
    stats = index_folder(store, folder)
    elapsed = time.time() - t0
    print(f"Scanned: {stats['scanned']} | Indexed: {stats['indexed']} | "
          f"Unchanged (skipped): {stats['skipped_unchanged']} | "
          f"Unsupported: {stats['unsupported']} | Failed: {stats['failed']}")
    print(f"Elapsed: {elapsed:.3f}s\n")

    print("=== Remembered folders (persists across restarts) ===")
    for f in store.get_watched_folders():
        print(f"  - {f['display_name']}  ({f['folder_path']})  last scanned: {f['last_scanned_at']}")
    print()

    print("=== Re-running the SAME indexing pass (proving incremental indexing) ===")
    t0 = time.time()
    stats2 = index_folder(store, folder)
    elapsed2 = time.time() - t0
    print(f"Scanned: {stats2['scanned']} | Indexed: {stats2['indexed']} | "
          f"Unchanged (skipped): {stats2['skipped_unchanged']}")
    print(f"Elapsed: {elapsed2:.3f}s  (should be near-zero re-parsing work)\n")

    print("=== Example questions against the knowledge base ===\n")
    session_id = "demo-session-1"
    questions = [
        "torque value M8 bolt",
        "required PPE gloves",
        "root cause fastener",
    ]
    last_doc_id = None
    for q in questions:
        print(f"USER: {q}")
        store.add_conversation_turn(ConversationTurn(session_id=session_id, role="user", content=q))

        hits = search_chunks(store, q)
        if not hits:
            print("  (no matches)\n")
            continue
        top = hits[0]
        citation_loc = f"p.{top['page_number']}" if top["page_number"] else (top["section_path"] or "")
        answer_preview = top["text"][:140].replace("\n", " ")
        print(f"ASSISTANT: [{top['element_type']}] \"{answer_preview}...\"")
        print(f"  Source: {top['filename']}" + (f" ({citation_loc})" if citation_loc else ""))
        if len(hits) > 1:
            print(f"  ({len(hits)} relevant chunks found total)")

        store.add_conversation_turn(ConversationTurn(
            session_id=session_id, role="assistant", content=answer_preview,
            active_topic=q, active_document_id=top["path"],
        ))
        print()

    print("=== Conversation memory recorded this session ===")
    for turn in store.get_recent_turns(session_id):
        print(f"  [{turn.role:>9}] {turn.content[:70]}")
    print()

    print("=== Cache stats ===")
    for ns, s in cache.stats().items():
        print(f"  {ns:<12} entries={s['entry_count']:<4} size={s['size_mb']}MB")


if __name__ == "__main__":
    main()
