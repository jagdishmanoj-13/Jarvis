"""
parser/chunker.py
==================

Turns a parser's flat list of `RawElement`s into the `Chunk` objects that
get embedded, indexed, searched, and cited.

Design decisions
-----------------
- Token counting has no tokenizer dependency (no tiktoken/sentencepiece —
  Citrix: no internet to install, and the future custom transformer will
  have its own tokenizer anyway that we can't predict yet). We approximate
  "tokens" as whitespace-split word count, which is within ~25% of true
  subword token counts for English technical text — good enough for
  chunk-sizing purposes since we're bounding for retrieval quality, not
  hitting an exact context-window limit.
- BODY_TEXT elements are merged greedily up to `chunk_size_tokens`, with
  the last `chunk_overlap_tokens` words of each chunk repeated at the start
  of the next (sliding-window overlap). This preserves context across a
  chunk boundary — a sentence about "the torque value" that gets split
  from its number three words later would otherwise become unanswerable.
- TABLE, CAPTION, FIGURE_LABEL, HYPERLINK, and METADATA elements are never
  merged with surrounding body text or with each other — each becomes its
  own chunk. Merging a table into a paragraph chunk would blur exactly the
  kind of structured content (torque tables, spec sheets) this assistant
  exists to answer questions about, and it would break the citation
  engine's ability to say "this came from Table 3" specifically.
- HEADING elements are not emitted as their own retrievable chunks (a
  heading alone, e.g. "3.2 Torque Specifications", is rarely a useful
  standalone answer) but their text is folded into `section_path` on the
  chunks that follow, which is already handled upstream by each parser.
- A chunk that would end up empty/whitespace-only after merging is
  dropped rather than stored, keeping the index and FTS table free of
  noise rows that could rank in searches without adding an answer.
"""

from __future__ import annotations

from typing import List

from config.settings import get_settings
from models.document import Chunk, ExtractionElementType
from parser.base_parser import ParsedDocument, RawElement

_NON_MERGEABLE_TYPES = {
    ExtractionElementType.TABLE,
    ExtractionElementType.CAPTION,
    ExtractionElementType.FIGURE_LABEL,
    ExtractionElementType.HYPERLINK,
    ExtractionElementType.METADATA,
    ExtractionElementType.OCR_TEXT,
}
_SKIP_TYPES = {ExtractionElementType.HEADING, ExtractionElementType.PAGE_NUMBER}


def _word_count(text: str) -> int:
    return len(text.split())


class TextChunker:
    def __init__(self, chunk_size_tokens: int | None = None, overlap_tokens: int | None = None):
        settings = get_settings()
        self.chunk_size = chunk_size_tokens or settings.chunk_size_tokens
        self.overlap = overlap_tokens or settings.chunk_overlap_tokens

    def chunk(self, document_id: str, parsed: ParsedDocument) -> List[Chunk]:
        chunks: List[Chunk] = []
        order = 0
        buffer_words: List[str] = []
        buffer_page: int | None = None
        buffer_section: str | None = None

        def flush_buffer():
            nonlocal order, buffer_words, buffer_page, buffer_section
            text = " ".join(buffer_words).strip()
            if text:
                chunks.append(Chunk(
                    document_id=document_id, text=text,
                    element_type=ExtractionElementType.BODY_TEXT,
                    page_number=buffer_page, section_path=buffer_section, order_index=order,
                ))
                order += 1
            buffer_words = []

        for element in parsed.elements:
            if element.element_type in _SKIP_TYPES:
                continue

            if element.element_type in _NON_MERGEABLE_TYPES:
                flush_buffer()
                if element.text.strip():
                    chunks.append(Chunk(
                        document_id=document_id, text=element.text.strip(),
                        element_type=element.element_type, page_number=element.page_number,
                        section_path=element.section_path, order_index=order, extra=element.extra,
                    ))
                    order += 1
                continue

            # BODY_TEXT: accumulate into the sliding-window buffer.
            words = element.text.split()
            if not words:
                continue

            if buffer_words and (buffer_page != element.page_number or buffer_section != element.section_path):
                # A page/section boundary always flushes, even if under the
                # size limit — a chunk should never silently blend content
                # from two different sections into one uncited blob.
                flush_buffer()

            buffer_page = element.page_number
            buffer_section = element.section_path
            buffer_words.extend(words)

            while len(buffer_words) >= self.chunk_size:
                head, rest = buffer_words[:self.chunk_size], buffer_words[self.chunk_size:]
                chunks.append(Chunk(
                    document_id=document_id, text=" ".join(head).strip(),
                    element_type=ExtractionElementType.BODY_TEXT,
                    page_number=buffer_page, section_path=buffer_section, order_index=order,
                ))
                order += 1
                overlap_words = head[-self.overlap:] if self.overlap else []
                buffer_words = overlap_words + rest

        flush_buffer()
        return chunks
