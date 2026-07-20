"""
database/metadata_store.py
===========================

The persistent local knowledge database (SQLite).

Design decisions
-----------------
- SQLite, as the spec explicitly requires, and because it is a zero-install,
  single-file, CPU-only database — ideal for a Citrix/offline Windows
  deployment where installing a server-based DB is not possible.
- One `MetadataStore` class wraps ALL sqlite access. Nothing else in the
  codebase is allowed to hold a `sqlite3.Connection` directly. This keeps
  transaction/locking behaviour centralised and makes it feasible to later
  swap SQLite for something else if requirements change.
- `sqlite3.connect(..., check_same_thread=False)` + a per-call context
  manager is used instead of a long-lived global connection, because the
  indexing pipeline runs on background threads (spec requirement:
  "multithreading", "background indexing", "non-blocking UI") and SQLite
  connections are not safe to share across threads without care. Opening a
  short-lived connection per operation, combined with WAL mode, gives safe
  concurrent read/write behaviour without a connection-pooling dependency.
- WAL (Write-Ahead Logging) journal mode is enabled so the Streamlit UI can
  keep reading (e.g. rendering search results) while a background indexing
  thread is writing new rows — this is what makes "non-blocking UI" possible
  at the database layer.
- Schema covers: `watched_folders` (remembered folders across restarts),
  `documents` (metadata table from the spec), `chunks` (extracted content
  units), and `conversation_turns` (memory layer). Vector embeddings
  themselves are NOT stored here — see `retrieval/vector_store_interface.py`
  (Phase 3) for the pluggable embedding store; this table only stores an
  `embedding_ref` key so the two stores can be joined.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Iterator, List, Optional

from config.settings import get_settings
from models.document import Chunk, ConversationTurn, DocumentMetadata, ExtractionElementType, IndexStatus
from utils.logger import get_logger

logger = get_logger(__name__)

SCHEMA_VERSION = 1

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS schema_info (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS watched_folders (
    folder_path TEXT PRIMARY KEY,
    added_at TEXT NOT NULL,
    last_scanned_at TEXT,
    is_active INTEGER NOT NULL DEFAULT 1,
    display_name TEXT
);

CREATE TABLE IF NOT EXISTS documents (
    document_id TEXT PRIMARY KEY,
    filename TEXT NOT NULL,
    extension TEXT NOT NULL,
    path TEXT NOT NULL UNIQUE,
    folder_path TEXT NOT NULL,
    created_date TEXT,
    modified_date TEXT,
    title TEXT,
    page_count INTEGER,
    language TEXT,
    content_hash TEXT,
    author TEXT,
    index_status TEXT NOT NULL DEFAULT 'pending',
    indexed_at TEXT,
    last_error TEXT,
    size_bytes INTEGER DEFAULT 0,
    domain_tags TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_documents_folder ON documents(folder_path);
CREATE INDEX IF NOT EXISTS idx_documents_status ON documents(index_status);
CREATE INDEX IF NOT EXISTS idx_documents_hash ON documents(content_hash);

CREATE TABLE IF NOT EXISTS chunks (
    chunk_id TEXT PRIMARY KEY,
    document_id TEXT NOT NULL REFERENCES documents(document_id) ON DELETE CASCADE,
    text TEXT NOT NULL,
    element_type TEXT NOT NULL,
    page_number INTEGER,
    section_path TEXT,
    order_index INTEGER DEFAULT 0,
    embedding_ref TEXT,
    extra_json TEXT DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_chunks_document ON chunks(document_id);

-- FTS5 virtual table for fast keyword/fuzzy search over chunk text.
-- content='chunks' means it stores no separate copy of the text (saves
-- disk on constrained Citrix profile quotas) and stays in sync via triggers.
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
    text, content='chunks', content_rowid='rowid'
);

CREATE TABLE IF NOT EXISTS conversation_turns (
    turn_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    cited_chunk_ids TEXT DEFAULT '',
    active_topic TEXT,
    active_document_id TEXT
);
CREATE INDEX IF NOT EXISTS idx_turns_session ON conversation_turns(session_id, timestamp);

CREATE TABLE IF NOT EXISTS app_state (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""

_FTS_TRIGGERS_SQL = """
CREATE TRIGGER IF NOT EXISTS chunks_ai AFTER INSERT ON chunks BEGIN
    INSERT INTO chunks_fts(rowid, text) VALUES (new.rowid, new.text);
END;
CREATE TRIGGER IF NOT EXISTS chunks_ad AFTER DELETE ON chunks BEGIN
    INSERT INTO chunks_fts(chunks_fts, rowid, text) VALUES ('delete', old.rowid, old.text);
END;
CREATE TRIGGER IF NOT EXISTS chunks_au AFTER UPDATE ON chunks BEGIN
    INSERT INTO chunks_fts(chunks_fts, rowid, text) VALUES ('delete', old.rowid, old.text);
    INSERT INTO chunks_fts(rowid, text) VALUES (new.rowid, new.text);
END;
"""


def _dt_to_str(dt: Optional[datetime]) -> Optional[str]:
    return dt.isoformat() if dt else None


def _str_to_dt(s: Optional[str]) -> Optional[datetime]:
    return datetime.fromisoformat(s) if s else None


class MetadataStore:
    """All SQLite access for JARVIS goes through this class."""

    def __init__(self, db_path: Optional[Path] = None):
        self.db_path = db_path or get_settings().db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(str(self.db_path), timeout=30, check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA foreign_keys=ON;")
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(_SCHEMA_SQL)
            try:
                conn.executescript(_FTS_TRIGGERS_SQL)
            except sqlite3.OperationalError as exc:
                # FTS5 unavailable in this SQLite build -> keyword search
                # will fall back to LIKE queries in the retrieval layer.
                logger.warning("FTS5 unavailable, full-text search will degrade to LIKE queries: %s", exc)
            conn.execute(
                "INSERT OR IGNORE INTO schema_info(key, value) VALUES ('version', ?)",
                (str(SCHEMA_VERSION),),
            )
        logger.info("MetadataStore initialised at %s", self.db_path)

    # ------------------------------------------------------------------
    # Watched folders — "JARVIS should already remember previously
    # indexed folders" on restart.
    # ------------------------------------------------------------------
    def add_watched_folder(self, folder_path: str, display_name: Optional[str] = None) -> None:
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO watched_folders(folder_path, added_at, is_active, display_name)
                   VALUES (?, ?, 1, ?)
                   ON CONFLICT(folder_path) DO UPDATE SET is_active=1, display_name=excluded.display_name""",
                (folder_path, datetime.utcnow().isoformat(), display_name or Path(folder_path).name),
            )

    def remove_watched_folder(self, folder_path: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE watched_folders SET is_active=0 WHERE folder_path=?", (folder_path,)
            )

    def touch_watched_folder(self, folder_path: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE watched_folders SET last_scanned_at=? WHERE folder_path=?",
                (datetime.utcnow().isoformat(), folder_path),
            )

    def get_watched_folders(self, active_only: bool = True) -> List[dict]:
        query = "SELECT * FROM watched_folders"
        if active_only:
            query += " WHERE is_active=1"
        query += " ORDER BY added_at DESC"
        with self._connect() as conn:
            return [dict(row) for row in conn.execute(query).fetchall()]

    # ------------------------------------------------------------------
    # Documents
    # ------------------------------------------------------------------
    def upsert_document(self, doc: DocumentMetadata, folder_path: str) -> None:
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO documents (
                    document_id, filename, extension, path, folder_path,
                    created_date, modified_date, title, page_count, language,
                    content_hash, author, index_status, indexed_at, last_error,
                    size_bytes, domain_tags
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(path) DO UPDATE SET
                    filename=excluded.filename, extension=excluded.extension,
                    created_date=excluded.created_date, modified_date=excluded.modified_date,
                    title=excluded.title, page_count=excluded.page_count,
                    language=excluded.language, content_hash=excluded.content_hash,
                    author=excluded.author, index_status=excluded.index_status,
                    indexed_at=excluded.indexed_at, last_error=excluded.last_error,
                    size_bytes=excluded.size_bytes, domain_tags=excluded.domain_tags
                """,
                (
                    doc.document_id, doc.filename, doc.extension, doc.path, folder_path,
                    _dt_to_str(doc.created_date), _dt_to_str(doc.modified_date), doc.title,
                    doc.page_count, doc.language, doc.content_hash, doc.author,
                    doc.index_status.value, _dt_to_str(doc.indexed_at), doc.last_error,
                    doc.size_bytes, ",".join(doc.domain_tags),
                ),
            )

    def get_document_by_path(self, path: str) -> Optional[DocumentMetadata]:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM documents WHERE path=?", (path,)).fetchone()
            return self._row_to_document(row) if row else None

    def get_document_by_id(self, document_id: str) -> Optional[DocumentMetadata]:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM documents WHERE document_id=?", (document_id,)).fetchone()
            return self._row_to_document(row) if row else None

    def list_documents(self, folder_path: Optional[str] = None,
                        status: Optional[IndexStatus] = None) -> List[DocumentMetadata]:
        query = "SELECT * FROM documents WHERE 1=1"
        params: list = []
        if folder_path:
            query += " AND folder_path=?"
            params.append(folder_path)
        if status:
            query += " AND index_status=?"
            params.append(status.value)
        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
            return [self._row_to_document(r) for r in rows]

    def delete_document(self, document_id: str) -> None:
        """Removes a document and its chunks (chunks cascade via FK)."""
        with self._connect() as conn:
            conn.execute("DELETE FROM documents WHERE document_id=?", (document_id,))

    def mark_documents_deleted(self, paths_still_present: List[str], folder_path: str) -> List[str]:
        """Given the set of paths currently on disk for a folder, mark any
        indexed document under that folder whose path is NOT in the set as
        deleted, and return their document_ids (spec: 'detect deleted files').
        """
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT document_id, path FROM documents WHERE folder_path=?", (folder_path,)
            ).fetchall()
            present = set(paths_still_present)
            removed_ids = []
            for row in rows:
                if row["path"] not in present:
                    conn.execute("DELETE FROM documents WHERE document_id=?", (row["document_id"],))
                    removed_ids.append(row["document_id"])
            return removed_ids

    @staticmethod
    def _row_to_document(row: sqlite3.Row) -> DocumentMetadata:
        return DocumentMetadata(
            document_id=row["document_id"], filename=row["filename"], extension=row["extension"],
            path=row["path"], created_date=_str_to_dt(row["created_date"]),
            modified_date=_str_to_dt(row["modified_date"]), title=row["title"],
            page_count=row["page_count"], language=row["language"], content_hash=row["content_hash"],
            author=row["author"], index_status=IndexStatus(row["index_status"]),
            indexed_at=_str_to_dt(row["indexed_at"]), last_error=row["last_error"],
            size_bytes=row["size_bytes"] or 0,
            domain_tags=[t for t in (row["domain_tags"] or "").split(",") if t],
        )

    # ------------------------------------------------------------------
    # Chunks
    # ------------------------------------------------------------------
    def replace_chunks_for_document(self, document_id: str, chunks: List[Chunk]) -> None:
        """Deletes old chunks for a document and inserts the new set.
        Used on re-index of a changed file so stale chunks never linger.
        """
        with self._connect() as conn:
            conn.execute("DELETE FROM chunks WHERE document_id=?", (document_id,))
            for c in chunks:
                import json as _json
                conn.execute(
                    """INSERT INTO chunks (chunk_id, document_id, text, element_type,
                        page_number, section_path, order_index, embedding_ref, extra_json)
                       VALUES (?,?,?,?,?,?,?,?,?)""",
                    (c.chunk_id, document_id, c.text, c.element_type.value, c.page_number,
                     c.section_path, c.order_index, c.extra.get("embedding_ref"),
                     _json.dumps(c.extra)),
                )

    def get_chunks_for_document(self, document_id: str) -> List[Chunk]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM chunks WHERE document_id=? ORDER BY order_index", (document_id,)
            ).fetchall()
            return [self._row_to_chunk(r) for r in rows]

    @staticmethod
    def _row_to_chunk(row: sqlite3.Row) -> Chunk:
        import json as _json
        return Chunk(
            chunk_id=row["chunk_id"], document_id=row["document_id"], text=row["text"],
            element_type=ExtractionElementType(row["element_type"]), page_number=row["page_number"],
            section_path=row["section_path"], order_index=row["order_index"] or 0,
            extra=_json.loads(row["extra_json"] or "{}"),
        )

    # ------------------------------------------------------------------
    # Conversation memory
    # ------------------------------------------------------------------
    def add_conversation_turn(self, turn: ConversationTurn) -> None:
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO conversation_turns (turn_id, session_id, role, content,
                    timestamp, cited_chunk_ids, active_topic, active_document_id)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (turn.turn_id, turn.session_id, turn.role, turn.content,
                 turn.timestamp.isoformat(), ",".join(turn.cited_chunk_ids),
                 turn.active_topic, turn.active_document_id),
            )

    def get_recent_turns(self, session_id: str, limit: int = 40) -> List[ConversationTurn]:
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT * FROM conversation_turns WHERE session_id=?
                   ORDER BY timestamp DESC LIMIT ?""",
                (session_id, limit),
            ).fetchall()
            turns = [self._row_to_turn(r) for r in rows]
            return list(reversed(turns))

    @staticmethod
    def _row_to_turn(row: sqlite3.Row) -> ConversationTurn:
        return ConversationTurn(
            turn_id=row["turn_id"], session_id=row["session_id"], role=row["role"],
            content=row["content"], timestamp=datetime.fromisoformat(row["timestamp"]),
            cited_chunk_ids=[c for c in (row["cited_chunk_ids"] or "").split(",") if c],
            active_topic=row["active_topic"], active_document_id=row["active_document_id"],
        )

    # ------------------------------------------------------------------
    # Misc app state (e.g. "last opened folder", "last project")
    # ------------------------------------------------------------------
    def set_app_state(self, key: str, value: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO app_state(key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )

    def get_app_state(self, key: str) -> Optional[str]:
        with self._connect() as conn:
            row = conn.execute("SELECT value FROM app_state WHERE key=?", (key,)).fetchone()
            return row["value"] if row else None
