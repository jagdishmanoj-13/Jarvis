"""
parser/base_parser.py
======================

The contract every format-specific parser implements.

Design decisions
-----------------
- `BaseParser` is a small ABC with exactly one required method, `parse()`.
  This is deliberate: the spec asks for "make adding new file types easy",
  so a new parser is "write one class, implement one method, register one
  extension" — no other file in the codebase needs to change.
- `ParsedDocument` is the parser's output: coarse document-level metadata
  (title/author/page_count/language) plus a flat list of `RawElement`s
  (text + structural type + page/section provenance). Parsers do NOT chunk
  text themselves — chunking (merging/splitting into retrieval-sized units)
  is a separate concern owned by `parser/chunker.py`, so chunking strategy
  can evolve independently of format-specific extraction logic.
- `RawElement` reuses the same `ExtractionElementType` vocabulary as the
  final `Chunk` model (models/document.py) so the chunker doesn't need a
  translation layer between "what a parser emits" and "what gets stored".
- Parsers must NOT raise on recoverable per-page/per-element failures (e.g.
  one corrupt page in an otherwise-fine 200-page PDF). They should log a
  warning, skip that element, and continue — a single bad page must never
  lose the other 199 pages. Parsers MAY raise for unrecoverable failures
  (file not found, wrong format, fully corrupt file); the indexer layer
  (Phase 3) is responsible for catching that and marking the document
  IndexStatus.FAILED with `last_error` set.
- `supported_extensions()` is a classmethod (not a module-level constant)
  so the registry can query it without instantiating a parser, and so a
  parser can theoretically compute its supported extensions dynamically
  (e.g. based on which optional dependency happens to be installed).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

from models.document import ExtractionElementType


@dataclass
class RawElement:
    """One structural unit extracted from a document, pre-chunking."""
    text: str
    element_type: ExtractionElementType = ExtractionElementType.BODY_TEXT
    page_number: Optional[int] = None
    section_path: Optional[str] = None
    order_index: int = 0
    extra: dict = field(default_factory=dict)


@dataclass
class ParsedDocument:
    """Full output of parsing one file, before chunking."""
    title: Optional[str] = None
    author: Optional[str] = None
    page_count: Optional[int] = None
    language: Optional[str] = None
    elements: List[RawElement] = field(default_factory=list)
    parser_warnings: List[str] = field(default_factory=list)


class ParserError(Exception):
    """Raised for unrecoverable parse failures (bad/corrupt/unsupported file)."""


class BaseParser(ABC):
    """Base class every format parser must extend."""

    @classmethod
    @abstractmethod
    def supported_extensions(cls) -> List[str]:
        """Lower-case extensions this parser handles, e.g. ['.pdf']."""
        raise NotImplementedError

    @abstractmethod
    def parse(self, path: Path) -> ParsedDocument:
        """Parse a single file into a ParsedDocument.

        Implementations should be defensive: catch per-element exceptions,
        record them in `parser_warnings`, and keep going rather than
        aborting the whole document over one bad table/page/slide.
        """
        raise NotImplementedError
