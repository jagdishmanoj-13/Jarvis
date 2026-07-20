"""
models/document.py
===================

Core domain models for anything document/knowledge related.

Design decisions
-----------------
- These are plain `dataclasses`, not ORM models. The database layer
  (`database/`) is responsible for converting to/from SQLite rows; keeping
  the domain models persistence-agnostic means the retrieval/reasoning/UI
  layers never import sqlite3 directly, and the storage backend could later
  be swapped (e.g. to an embedded document DB) without touching them.
- `DocumentMetadata` intentionally mirrors every field the spec asked for
  (filename, extension, path, created/modified dates, title, page_count,
  language, hash, author) plus a few operational fields (`indexed_at`,
  `index_status`, `content_hash`) needed for incremental indexing.
- `Chunk` is the atomic retrieval unit. Parsers don't return one giant blob
  of text per document; they return a list of `Chunk`s that already carry
  structural provenance (page number, section path, chunk type) so the
  citation engine can point back to an exact page/table/figure, not just
  "somewhere in this file".
- `ExtractionElementType` enumerates the structural element types the spec
  requires (tables, headers, footers, captions, OCR text, figure labels,
  etc.) so every parser produces a consistent vocabulary the reasoning
  engine can reason over uniformly regardless of source file format.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional


class IndexStatus(str, Enum):
    PENDING = "pending"
    INDEXING = "indexing"
    INDEXED = "indexed"
    FAILED = "failed"
    DELETED = "deleted"


class ExtractionElementType(str, Enum):
    """Structural vocabulary every parser must map its output onto."""
    BODY_TEXT = "body_text"
    HEADING = "heading"
    HEADER = "header"
    FOOTER = "footer"
    TABLE = "table"
    CAPTION = "caption"
    FIGURE_LABEL = "figure_label"
    OCR_TEXT = "ocr_text"
    HYPERLINK = "hyperlink"
    REVISION_NOTE = "revision_note"
    PAGE_NUMBER = "page_number"
    METADATA = "metadata"


@dataclass
class DocumentMetadata:
    """One row of the spec's 'store metadata for every document' requirement."""
    document_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    filename: str = ""
    extension: str = ""
    path: str = ""
    created_date: Optional[datetime] = None
    modified_date: Optional[datetime] = None
    title: Optional[str] = None
    page_count: Optional[int] = None
    language: Optional[str] = None
    content_hash: Optional[str] = None
    author: Optional[str] = None

    # Operational / indexing bookkeeping
    index_status: IndexStatus = IndexStatus.PENDING
    indexed_at: Optional[datetime] = None
    last_error: Optional[str] = None
    size_bytes: int = 0
    domain_tags: List[str] = field(default_factory=list)  # e.g. ["Aerospace", "SOP"]

    @property
    def path_obj(self) -> Path:
        return Path(self.path)


@dataclass
class Chunk:
    """The atomic retrieval + citation unit produced by parsers."""
    chunk_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    document_id: str = ""
    text: str = ""
    element_type: ExtractionElementType = ExtractionElementType.BODY_TEXT
    page_number: Optional[int] = None
    section_path: Optional[str] = None  # e.g. "3.2 Torque Specifications"
    order_index: int = 0  # position within the document, for ordering/citation
    extra: Dict[str, Any] = field(default_factory=dict)  # table cells, hyperlink target, etc.

    def citation_label(self, filename: str) -> str:
        loc = f"p.{self.page_number}" if self.page_number else self.section_path or ""
        return f"{filename}" + (f" ({loc})" if loc else "")


@dataclass
class SearchResult:
    """A single scored hit returned by the retrieval layer."""
    chunk: Chunk
    document: DocumentMetadata
    score: float
    match_reasons: List[str] = field(default_factory=list)  # e.g. ["keyword:torque", "fuzzy"]


@dataclass
class ConversationTurn:
    """One turn of dialogue, used by the memory layer."""
    turn_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    session_id: str = ""
    role: str = "user"  # "user" | "assistant"
    content: str = ""
    timestamp: datetime = field(default_factory=datetime.utcnow)
    cited_chunk_ids: List[str] = field(default_factory=list)
    active_topic: Optional[str] = None
    active_document_id: Optional[str] = None
