"""
parser/registry.py
===================

Maps a file extension to the parser instance that handles it.

Design decisions
-----------------
- A single dict built once at import time from every concrete parser's
  `supported_extensions()`. Adding a new file type is: write the parser
  class, add one line here. Nothing else in the system (indexer, UI,
  cache) needs to know the registry exists — they just call
  `get_parser_for(path)`.
- Parsers that need an optional third-party dependency that might be
  missing on a locked-down Citrix machine are imported inside a try/except
  at registration time. If the import fails, that extension is registered
  under `UnavailableParser` instead of crashing the whole app — the user
  sees "OCR/legacy .doc support isn't installed" for that specific file
  rather than JARVIS failing to start.
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict, Optional, Type

from parser.base_parser import BaseParser, ParsedDocument, ParserError
from utils.logger import get_logger

logger = get_logger(__name__)


class UnavailableParser(BaseParser):
    """Placeholder registered when a format's real parser can't be loaded
    (missing optional dependency). Fails loudly and specifically instead of
    silently producing empty/fake content, so the indexer can surface a
    clear, actionable error to the user for that file.
    """

    def __init__(self, extension: str, reason: str):
        self.extension = extension
        self.reason = reason

    @classmethod
    def supported_extensions(cls):
        return []

    def parse(self, path: Path) -> ParsedDocument:
        raise ParserError(
            f"No parser available for '{self.extension}' files ({self.reason}). "
            f"File skipped: {path}"
        )


_registry: Dict[str, BaseParser] = {}


def register(parser_cls: Type[BaseParser]) -> None:
    instance = parser_cls()
    for ext in parser_cls.supported_extensions():
        ext = ext.lower()
        if ext in _registry and not isinstance(_registry[ext], UnavailableParser):
            logger.warning("Extension %s already registered to %s; overriding with %s",
                            ext, type(_registry[ext]).__name__, parser_cls.__name__)
        _registry[ext] = instance


def register_unavailable(extensions, reason: str) -> None:
    for ext in extensions:
        ext = ext.lower()
        if ext not in _registry:
            _registry[ext] = UnavailableParser(ext, reason)


def get_parser_for(path: Path) -> Optional[BaseParser]:
    return _registry.get(path.suffix.lower())


def registered_extensions() -> list:
    return sorted(_registry.keys())


def _build_registry() -> None:
    """Imports and registers every known parser. Each import is isolated so
    one broken/missing optional dependency can't take down the others.
    """
    # --- Always-available, stdlib/lightweight-dependency parsers ---
    try:
        from parser.text_family_parser import (
            PlainTextParser, MarkdownParser, CodeParser, StructuredTextParser,
            HtmlParser, XmlParser, RtfParser,
        )
        register(PlainTextParser)
        register(MarkdownParser)
        register(CodeParser)
        register(StructuredTextParser)
        register(HtmlParser)
        register(XmlParser)
        register(RtfParser)
    except ImportError as exc:
        logger.error("Failed to load text-family parsers: %s", exc)

    # --- CSV / Excel ---
    try:
        from parser.tabular_parser import CsvParser, ExcelParser
        register(CsvParser)
        register(ExcelParser)
    except ImportError as exc:
        register_unavailable([".xls", ".xlsx"], "openpyxl not installed")
        logger.warning("Excel parser unavailable: %s", exc)

    # --- PDF ---
    try:
        from parser.pdf_parser import PdfParser
        register(PdfParser)
    except ImportError as exc:
        register_unavailable([".pdf"], "pypdf not installed")
        logger.warning("PDF parser unavailable: %s", exc)

    # --- DOCX ---
    try:
        from parser.docx_parser import DocxParser
        register(DocxParser)
    except ImportError as exc:
        register_unavailable([".docx"], "python-docx not installed")
        logger.warning("DOCX parser unavailable: %s", exc)

    # --- PPTX ---
    try:
        from parser.pptx_parser import PptxParser
        register(PptxParser)
    except ImportError as exc:
        register_unavailable([".pptx"], "python-pptx not installed")
        logger.warning("PPTX parser unavailable: %s", exc)

    # --- Images (scanned pages, nameplates, labels) — depends on the OCR
    #     engine being genuinely usable on THIS machine at runtime, not
    #     just on a Python package being importable, so this check happens
    #     here rather than via a simple ImportError try/except.
    try:
        from ocr.ocr_engine import get_ocr_engine
        from parser.image_parser import ImageParser
        if get_ocr_engine().is_available:
            register(ImageParser)
        else:
            register_unavailable(
                [".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp"],
                "OCR engine (tesseract) not found on this machine",
            )
    except ImportError as exc:
        register_unavailable([".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp"],
                              f"OCR dependencies not installed ({exc})")
        logger.warning("Image/OCR parser unavailable: %s", exc)

    # --- Legacy binary formats needing external tools not guaranteed on
    #     a Citrix box (no internet, limited installs). Registered as
    #     explicitly unavailable rather than silently mishandled; the
    #     indexer will surface a clear per-file error. A real deployment
    #     can plug in e.g. a LibreOffice headless converter if available.
    register_unavailable([".doc"], "legacy .doc requires an external converter (e.g. LibreOffice headless), not bundled")
    register_unavailable([".ppt"], "legacy .ppt requires an external converter (e.g. LibreOffice headless), not bundled")

    # --- Formats handled by dedicated pipeline steps, not the parser registry ---
    register_unavailable([".zip"], "expanded by core.archive_service before parsing, not handled as a text parser")
    register_unavailable([".eml", ".msg"], "email parser scheduled for a later phase")


_build_registry()
