"""
parser/docx_parser.py
======================

Word (.docx) parsing via `python-docx`.

Design decisions
-----------------
- python-docx exposes the document body as a flat sequence of Paragraph
  and Table objects but doesn't give a single ordered iterator over both
  interleaved — we walk the underlying XML body element order ourselves
  (`document.element.body`) so tables appear in their true position
  relative to surrounding paragraphs, which matters for section_path
  breadcrumbs and for citations reading naturally ("the table right after
  the torque section" rather than "all tables, then all text").
- Heading detection uses the paragraph's style name ("Heading 1", "Heading
  2", ...) which is how Word itself represents document structure — this
  is far more reliable than guessing from font size/bold.
- Hyperlinks in python-docx live in the paragraph's XML as `w:hyperlink`
  elements referencing a relationship ID; we resolve those via
  `paragraph.part.rels` to get the actual URL (spec: "embedded
  hyperlinks").
- Tables are rendered the same pipe-delimited way as the tabular parser
  for consistency of what a "TABLE" element looks like across the whole
  system, regardless of source format.
- No support for tracked-changes/comments extraction in this phase (the
  spec's structure implies this could matter for engineering documents
  under revision control) — flagged as a known follow-up rather than
  faked.
"""

from __future__ import annotations

from pathlib import Path
from typing import List, Optional

from models.document import ExtractionElementType
from parser.base_parser import BaseParser, ParsedDocument, ParserError, RawElement
from utils.logger import get_logger

logger = get_logger(__name__)

_HEADING_STYLE_PREFIX = "Heading"


class DocxParser(BaseParser):
    @classmethod
    def supported_extensions(cls) -> List[str]:
        return [".docx"]

    def parse(self, path: Path) -> ParsedDocument:
        import docx
        from docx.document import Document as DocxDocument
        from docx.oxml.ns import qn
        from docx.table import Table
        from docx.text.paragraph import Paragraph

        try:
            document: DocxDocument = docx.Document(str(path))
        except Exception as exc:
            raise ParserError(f"Could not open DOCX {path}: {exc}")

        core_props = document.core_properties
        doc = ParsedDocument(
            title=core_props.title or path.stem,
            author=core_props.author,
            page_count=None,  # docx has no reliable page count without rendering
        )

        section_stack: List[str] = []
        order = 0

        def iter_block_items(parent):
            """Yield Paragraph/Table objects in true document order."""
            parent_elm = parent.element.body
            for child in parent_elm.iterchildren():
                if child.tag == qn("w:p"):
                    yield Paragraph(child, parent)
                elif child.tag == qn("w:tbl"):
                    yield Table(child, parent)

        for block in iter_block_items(document):
            if isinstance(block, Paragraph):
                text = block.text.strip()
                if not text:
                    continue
                style_name = block.style.name if block.style else ""
                if style_name and style_name.startswith(_HEADING_STYLE_PREFIX):
                    try:
                        level = int("".join(ch for ch in style_name if ch.isdigit()) or "1")
                    except ValueError:
                        level = 1
                    section_stack = section_stack[:level - 1] + [text]
                    doc.elements.append(RawElement(
                        text=text, element_type=ExtractionElementType.HEADING,
                        section_path=" > ".join(section_stack), order_index=order,
                    ))
                elif style_name in ("Caption",):
                    doc.elements.append(RawElement(
                        text=text, element_type=ExtractionElementType.CAPTION,
                        section_path=" > ".join(section_stack), order_index=order,
                    ))
                else:
                    doc.elements.append(RawElement(
                        text=text, element_type=ExtractionElementType.BODY_TEXT,
                        section_path=" > ".join(section_stack) or None, order_index=order,
                    ))
                order += 1

                # Hyperlinks embedded in this paragraph's runs
                for hyperlink_elm in block._p.findall(qn("w:hyperlink")):
                    rid = hyperlink_elm.get(qn("r:id"))
                    if rid and rid in block.part.rels:
                        url = block.part.rels[rid].target_ref
                        link_text = "".join(node.text or "" for node in hyperlink_elm.iter(qn("w:t")))
                        if url:
                            doc.elements.append(RawElement(
                                text=link_text or url, element_type=ExtractionElementType.HYPERLINK,
                                section_path=" > ".join(section_stack) or None, order_index=order,
                                extra={"href": url},
                            ))
                            order += 1

            elif isinstance(block, Table):
                rows_text = []
                for row in block.rows:
                    cells = [cell.text.strip() for cell in row.cells]
                    rows_text.append(" | ".join(cells))
                rendered = "\n".join(rows_text)
                if rendered.strip():
                    doc.elements.append(RawElement(
                        text=rendered, element_type=ExtractionElementType.TABLE,
                        section_path=" > ".join(section_stack) or None, order_index=order,
                    ))
                    order += 1

        return doc
