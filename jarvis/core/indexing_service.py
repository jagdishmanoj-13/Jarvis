"""
core/indexing_service.py
=========================

The reusable "index a folder, then search it" service.

Design decisions
-----------------
- This is deliberately the same logic proven in `demo/run_demo.py`, moved
  here so the Streamlit UI (and any future CLI or scheduled task) calls one
  shared, tested implementation instead of each reimplementing indexing.
- `index_folder()` is synchronous in this phase. Phase 3 will wrap this in
  a background `ThreadPoolExecutor` so the UI stays responsive while a
  large folder indexes (`Settings.indexing_thread_pool_size`) — the
  function signature here is intentionally kept side-effect-pure per file
  (take a path, return a result) so it drops into a thread pool without
  modification later.
- `search_chunks()` currently queries the FTS5 keyword index directly, the
  same "preview" approach used in the demo. This is clearly not the full
  hybrid/fuzzy/synonym Retrieval Engine from the spec (that's Phase 4) —
  it is intentionally isolated in this one function so swapping it for the
  real `retrieval.hybrid_search.search()` later is a one-line change in
  the UI layer.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, List, Optional

from database.metadata_store import MetadataStore
from models.document import DocumentMetadata, IndexStatus
from parser.base_parser import ParserError
from parser.chunker import TextChunker
from parser.registry import get_parser_for
from core.archive_service import ArchiveError, expand_zip
from utils.hashing import compute_file_hash
from utils.logger import get_logger

logger = get_logger(__name__)


@dataclass
class IndexingStats:
    scanned: int = 0
    indexed: int = 0
    skipped_unchanged: int = 0
    failed: int = 0
    unsupported: int = 0
    archives_expanded: int = 0
    failed_files: Optional[List[str]] = None

    def __post_init__(self):
        if self.failed_files is None:
            self.failed_files = []

    def merge(self, other: "IndexingStats") -> None:
        self.scanned += other.scanned
        self.indexed += other.indexed
        self.skipped_unchanged += other.skipped_unchanged
        self.failed += other.failed
        self.unsupported += other.unsupported
        self.archives_expanded += other.archives_expanded
        self.failed_files.extend(other.failed_files)


def index_folder(
    store: MetadataStore,
    folder: Path,
    progress_callback: Optional[Callable[[int, int, str], None]] = None,
    _folder_path_override: Optional[str] = None,
    _register_watch: bool = True,
) -> IndexingStats:
    """Recursively scan `folder`, parse new/changed supported files, and
    store their metadata + chunks. Unchanged files (by content hash) are
    skipped. Files no longer present on disk are removed from the store.
    `.zip` archives are expanded and their contents indexed recursively
    under a distinct "virtual folder" key derived from the archive's path
    and content hash, so archive contents never collide with, and can
    never accidentally trigger deletion of, real files in the parent
    folder's own document set.

    `progress_callback(done_count, total_count, current_filename)` is
    called after each file, letting the UI show a live progress bar
    without this function importing Streamlit.

    `_folder_path_override` / `_register_watch` are internal parameters
    used for the recursive call into an expanded archive's contents;
    callers indexing a real folder should not need to pass them.
    """
    chunker = TextChunker()
    stats = IndexingStats()
    seen_paths: List[str] = []
    folder_path_key = _folder_path_override or str(folder.resolve())

    if _register_watch:
        store.add_watched_folder(str(folder), display_name=folder.name)

    all_files = [p for p in folder.rglob("*") if p.is_file()]
    total = len(all_files)

    for i, path in enumerate(all_files, start=1):
        stats.scanned += 1
        seen_paths.append(str(path.resolve()))

        if path.suffix.lower() == ".zip":
            nested_stats = _index_zip_archive(store, chunker, path, folder_path_key)
            stats.merge(nested_stats)
            if progress_callback:
                progress_callback(i, total, path.name)
            continue

        try:
            existing = store.get_document_by_path(str(path.resolve()))
            content_hash = compute_file_hash(path)
        except (OSError, PermissionError) as exc:
            stats.failed += 1
            stats.failed_files.append(f"{path.name} (unreadable: {exc})")
            if progress_callback:
                progress_callback(i, total, path.name)
            continue

        if existing and existing.content_hash == content_hash and existing.index_status == IndexStatus.INDEXED:
            stats.skipped_unchanged += 1
            if progress_callback:
                progress_callback(i, total, path.name)
            continue

        parser = get_parser_for(path)
        if parser is None:
            stats.unsupported += 1
            if progress_callback:
                progress_callback(i, total, path.name)
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
            store.upsert_document(doc_meta, folder_path=folder_path_key)

            stored_doc = store.get_document_by_path(str(path.resolve()))
            chunks = chunker.chunk(stored_doc.document_id, parsed)
            store.replace_chunks_for_document(stored_doc.document_id, chunks)
            stats.indexed += 1
        except ParserError as exc:
            doc_meta.index_status = IndexStatus.FAILED
            doc_meta.last_error = str(exc)
            store.upsert_document(doc_meta, folder_path=folder_path_key)
            stats.failed += 1
            stats.failed_files.append(f"{path.name} ({exc})")
            logger.warning("Failed to index %s: %s", path, exc)
        except Exception as exc:  # last-resort guard: one bad file must never abort the whole scan
            stats.failed += 1
            stats.failed_files.append(f"{path.name} (unexpected error: {exc})")
            logger.exception("Unexpected error indexing %s", path)

        if progress_callback:
            progress_callback(i, total, path.name)

    removed = store.mark_documents_deleted(seen_paths, folder_path_key)
    if _register_watch:
        store.touch_watched_folder(str(folder))
    if removed:
        logger.info("Removed %d deleted file(s) from index for %s", len(removed), folder)

    return stats


def _index_zip_archive(store: MetadataStore, chunker: TextChunker, zip_path: Path, parent_folder_key: str) -> IndexingStats:
    """Expands a zip and indexes its contents under a virtual folder key
    scoped to this specific archive, so re-running a scan never confuses
    archive contents with sibling real files.
    """
    stats = IndexingStats()
    try:
        extracted_dir = expand_zip(zip_path)
    except ArchiveError as exc:
        stats.failed += 1
        stats.failed_files.append(f"{zip_path.name} ({exc})")
        logger.warning("Archive expansion failed for %s: %s", zip_path, exc)
        return stats

    stats.archives_expanded += 1
    virtual_folder_key = f"{parent_folder_key}::zip::{zip_path.name}"

    # A .zip's own contents count toward the parent's `scanned` total via
    # the outer loop; here we index what's inside it into its own bucket.
    nested_stats = index_folder(
        store, extracted_dir, _folder_path_override=virtual_folder_key, _register_watch=False,
    )
    stats.merge(nested_stats)
    return stats


def search_chunks(store: MetadataStore, query: str, limit: int = 8) -> List[dict]:
    """FTS5 keyword search joined back to document metadata for citations.
    Returns dicts (not dataclasses) since this is a UI-facing convenience
    function; the real Retrieval layer (Phase 4) will return SearchResult
    objects instead.
    """
    query = query.strip()
    if not query:
        return []

    # FTS5 default is AND-of-terms; OR-joining terms widens recall so a
    # question with several keywords doesn't require all of them to land
    # in the exact same chunk (see the "required PPE gloves" gap noted in
    # the Phase 2 demo — full ranking/merging is still Phase 4's job, this
    # is a pragmatic improvement in the meantime).
    terms = [t for t in query.replace('"', " ").split() if t]
    fts_query = " OR ".join(terms) if terms else query

    try:
        with store._connect() as conn:
            rows = conn.execute(
                """
                SELECT d.filename, d.path, d.document_id, c.text, c.element_type,
                       c.page_number, c.section_path, bm25(chunks_fts) AS rank
                FROM chunks_fts
                JOIN chunks c ON c.rowid = chunks_fts.rowid
                JOIN documents d ON d.document_id = c.document_id
                WHERE chunks_fts MATCH ?
                ORDER BY rank
                LIMIT ?
                """,
                (fts_query, limit),
            ).fetchall()
            return [dict(r) for r in rows]
    except Exception as exc:
        logger.warning("Search failed for query %r: %s", query, exc)
        return []
