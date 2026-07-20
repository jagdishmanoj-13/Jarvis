"""
parser/image_parser.py
========================

Parses standalone image files (.png, .jpg, .tif, .bmp, ...) by running
them through the pluggable OCR engine.

Design decisions
-----------------
- This parser is registered separately from `registry.py`'s normal
  auto-import list (see `parser/registry.py`) because it depends on the
  OCR engine's *runtime* availability, not just an importable Python
  package — `ocr.get_ocr_engine()` may return a working engine or a
  `NullOCREngine` depending on whether the `tesseract` binary happens to
  be installed on this particular machine. The registry checks that at
  registration time and only wires up `ImageParser` if OCR is real,
  otherwise it registers the extensions as `UnavailableParser` with a
  clear reason.
- Every image is capped at `_MAX_DIMENSION` on its longest side before
  OCR (downscaled if larger, upscaled if much smaller — Tesseract accuracy
  drops sharply below ~150 DPI-equivalent text height). This bounds CPU
  time per image on a Citrix VM with no GPU, since OCR is by far the most
  expensive step in the whole pipeline.
- Low-confidence OCR results (`mean_confidence` below `_LOW_CONFIDENCE`)
  are still stored (better than nothing) but flagged via
  `extra={"low_confidence": True}` so the UI/citation engine can warn the
  user to double check against the original image rather than presenting
  possibly-garbled OCR text with false authority.
"""

from __future__ import annotations

from pathlib import Path
from typing import List

from models.document import ExtractionElementType
from ocr.ocr_engine import get_ocr_engine
from parser.base_parser import BaseParser, ParsedDocument, ParserError, RawElement
from utils.logger import get_logger

logger = get_logger(__name__)

_MAX_DIMENSION = 3000
_LOW_CONFIDENCE = 60.0


class ImageParser(BaseParser):
    @classmethod
    def supported_extensions(cls) -> List[str]:
        return [".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp"]

    def parse(self, path: Path) -> ParsedDocument:
        from PIL import Image

        engine = get_ocr_engine()
        if not engine.is_available:
            raise ParserError(f"OCR engine unavailable; cannot extract text from image {path}")

        try:
            image = Image.open(path)
            image.load()
        except Exception as exc:
            raise ParserError(f"Could not open image {path}: {exc}")

        image = self._normalize_size(image)
        result = engine.ocr_image(image)

        doc = ParsedDocument(title=path.stem, page_count=1)
        if result.warnings:
            doc.parser_warnings.extend(result.warnings)

        text = result.text.strip()
        if text:
            doc.elements.append(RawElement(
                text=text, element_type=ExtractionElementType.OCR_TEXT, order_index=0,
                extra={
                    "ocr_engine": result.engine_name,
                    "ocr_confidence": result.mean_confidence,
                    "low_confidence": bool(result.mean_confidence is not None and result.mean_confidence < _LOW_CONFIDENCE),
                },
            ))
        else:
            doc.parser_warnings.append("OCR produced no text (blank image or unreadable content).")

        return doc

    @staticmethod
    def _normalize_size(image):
        width, height = image.size
        longest = max(width, height)
        if longest > _MAX_DIMENSION:
            scale = _MAX_DIMENSION / longest
            image = image.resize((int(width * scale), int(height * scale)))
        return image
