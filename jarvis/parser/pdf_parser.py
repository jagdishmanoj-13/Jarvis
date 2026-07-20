"""
parser/pdf_parser.py
=====================

PDF parsing via `pypdf`.

Design decisions
-----------------
- `pypdf` (pure-Python, no native binary dependency) is used instead of
  e.g. `PyMuPDF`/`fitz`, which ships compiled binaries that are more likely
  to be blocked by Citrix endpoint policy or fail to install without admin
  rights. `pdfplumber`/`pdfminer.six` are available in this sandbox too but
  pull in more transitive dependencies than pypdf for the base text-
  extraction case; they remain a reasonable future upgrade for a dedicated
  table-extraction pass since pypdf's table support is weak.
- Per-page extraction is wrapped in try/except: a single malformed page
  must not abort extraction of the other pages (see base_parser design
  notes). Each page failure is recorded in `parser_warnings`.
- Pages whose extracted text is suspiciously short relative to the page
  (below `_SCANNED_PAGE_CHAR_THRESHOLD`) are flagged via
  `extra={"likely_scanned": True}` on that page's element. This is how the
  spec's "scanned PDFs" requirement is wired up: the Phase-3 OCR pipeline
  can scan for this flag and run OCR ONLY on the pages that need it,
  rather than OCR-ing every page of every PDF (expensive on a CPU-only
  Citrix VM).
- Document metadata (title/author) comes from the PDF's `/Info` dictionary
  when present, falling back to the filename.
"""

from __future__ import annotations

from pathlib import Path
from typing import List

from models.document import ExtractionElementType
from ocr.ocr_engine import get_ocr_engine
from parser.base_parser import BaseParser, ParsedDocument, ParserError, RawElement
from utils.logger import get_logger

logger = get_logger(__name__)

_SCANNED_PAGE_CHAR_THRESHOLD = 20  # fewer extracted chars than this => probably a scanned/image page
_OCR_RENDER_DPI = 200  # balances text legibility against CPU cost on a no-GPU Citrix VM


class PdfParser(BaseParser):
    """Note: `ocr_scanned_pages` defaults to True. Rendering a page to an
    image and running Tesseract on it is far more expensive than plain text
    extraction, so this is skippable (`PdfParser(ocr_scanned_pages=False)`)
    for callers that want a fast first pass and can OCR selectively later.
    """

    def __init__(self, ocr_scanned_pages: bool = True):
        self.ocr_scanned_pages = ocr_scanned_pages

    @classmethod
    def supported_extensions(cls) -> List[str]:
        return [".pdf"]

    def parse(self, path: Path) -> ParsedDocument:
        from pypdf import PdfReader
        from pypdf.errors import PdfReadError

        try:
            reader = PdfReader(str(path), strict=False)
        except (PdfReadError, OSError) as exc:
            raise ParserError(f"Could not open PDF {path}: {exc}")

        if reader.is_encrypted:
            try:
                reader.decrypt("")  # try an empty password (common for "protected" internal docs)
            except Exception:
                raise ParserError(f"PDF {path} is password-protected; cannot extract text")

        info = reader.metadata or {}
        title = (info.title if hasattr(info, "title") else None) or path.stem
        author = info.author if hasattr(info, "author") else None

        doc = ParsedDocument(title=title, author=author, page_count=len(reader.pages))
        order = 0

        for page_index, page in enumerate(reader.pages, start=1):
            try:
                text = page.extract_text() or ""
            except Exception as exc:
                doc.parser_warnings.append(f"Page {page_index}: extraction failed ({exc}); skipped")
                logger.warning("PDF %s page %d extraction failed: %s", path, page_index, exc)
                continue

            stripped = text.strip()
            likely_scanned = len(stripped) < _SCANNED_PAGE_CHAR_THRESHOLD

            if likely_scanned:
                ocr_text, ocr_warning = self._ocr_page_if_possible(path, page_index)
                if ocr_text:
                    doc.elements.append(RawElement(
                        text=ocr_text, element_type=ExtractionElementType.OCR_TEXT,
                        page_number=page_index, order_index=order,
                        extra={"likely_scanned": True},
                    ))
                else:
                    doc.elements.append(RawElement(
                        text=stripped, element_type=ExtractionElementType.BODY_TEXT,
                        page_number=page_index, order_index=order,
                        extra={"likely_scanned": True},
                    ))
                if ocr_warning:
                    doc.parser_warnings.append(ocr_warning)
            elif stripped:
                doc.elements.append(RawElement(
                    text=stripped, element_type=ExtractionElementType.BODY_TEXT,
                    page_number=page_index, order_index=order,
                ))
            order += 1

            # Hyperlinks (annotations) — spec requires "embedded hyperlinks"
            try:
                annotations = page.get("/Annots")
                if annotations:
                    for annot in annotations:
                        obj = annot.get_object()
                        uri = obj.get("/A", {}).get("/URI") if obj.get("/A") else None
                        if uri:
                            doc.elements.append(RawElement(
                                text=str(uri), element_type=ExtractionElementType.HYPERLINK,
                                page_number=page_index, order_index=order,
                                extra={"href": str(uri)},
                            ))
                            order += 1
            except Exception:
                pass  # annotation parsing is best-effort; never fail the page over it

            doc.elements.append(RawElement(
                text=str(page_index), element_type=ExtractionElementType.PAGE_NUMBER,
                page_number=page_index, order_index=order,
            ))
            order += 1

        return doc

    def _ocr_page_if_possible(self, path: Path, page_index: int):
        """Renders one PDF page to an image and OCRs it. Returns
        (text_or_none, warning_or_none). Never raises: OCR failure on a
        single scanned page must not abort extraction of the rest of the
        document.
        """
        if not self.ocr_scanned_pages:
            return None, None

        engine = get_ocr_engine()
        if not engine.is_available:
            return None, f"Page {page_index}: appears scanned but OCR engine is unavailable on this machine"

        try:
            from pdf2image import convert_from_path
            images = convert_from_path(str(path), dpi=_OCR_RENDER_DPI,
                                        first_page=page_index, last_page=page_index)
            if not images:
                return None, f"Page {page_index}: could not render page image for OCR"
            result = engine.ocr_image(images[0])
            if not result.text.strip():
                return None, f"Page {page_index}: OCR ran but found no text"
            return result.text.strip(), None
        except Exception as exc:
            logger.warning("OCR failed for %s page %d: %s", path, page_index, exc)
            return None, f"Page {page_index}: OCR failed ({exc})"
