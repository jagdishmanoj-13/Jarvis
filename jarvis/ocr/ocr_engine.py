"""
ocr/ocr_engine.py
==================

Pluggable OCR interface, mirroring the same "interface now, swap the
implementation later" pattern used for the future LanguageGenerationEngine.

Design decisions
-----------------
- `BaseOCREngine` defines one method, `ocr_image(image) -> OCRResult`. This
  keeps OCR engine choice (Tesseract today; a Citrix deployment could swap
  in a commercial CPU-only OCR SDK later) fully isolated from the PDF/image
  parsers that call it.
- `TesseractOCREngine` wraps `pytesseract`, which shells out to the
  `tesseract` binary. This is the right choice for a CPU-only, offline,
  install-restricted Citrix box: Tesseract is a single portable executable
  (no GPU, no service, no license server) and many enterprises already
  have it approved/whitelisted for OCR use.
- If the `tesseract` binary is not on PATH, or `pytesseract` isn't
  installed, `get_ocr_engine()` returns a `NullOCREngine` instead of
  raising — OCR being unavailable should degrade to "scanned pages aren't
  searchable yet" for that one document, not crash indexing of every other
  file. `NullOCREngine.is_available` lets callers detect this and skip OCR
  work entirely rather than calling it just to get an empty result.
- Confidence scores are captured per word from Tesseract's TSV output and
  averaged, so low-confidence OCR results can later be flagged in the UI
  (e.g. "this text was OCR'd from a scan with 61% confidence — verify
  against the source image") rather than presented with false certainty.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from typing import List, Optional

from utils.logger import get_logger

logger = get_logger(__name__)


@dataclass
class OCRResult:
    text: str
    mean_confidence: Optional[float] = None  # 0-100, None if unavailable
    engine_name: str = "none"
    warnings: List[str] = field(default_factory=list)


class BaseOCREngine:
    is_available: bool = False

    def ocr_image(self, image) -> OCRResult:  # image: PIL.Image.Image
        raise NotImplementedError


class NullOCREngine(BaseOCREngine):
    """Used when no real OCR engine is available on this machine."""
    is_available = False

    def ocr_image(self, image) -> OCRResult:
        return OCRResult(text="", mean_confidence=None, engine_name="none",
                          warnings=["OCR engine unavailable on this machine (tesseract not found)."])


class TesseractOCREngine(BaseOCREngine):
    is_available = True

    def __init__(self, lang: str = "eng"):
        self.lang = lang

    def ocr_image(self, image) -> OCRResult:
        import pytesseract

        try:
            data = pytesseract.image_to_data(image, lang=self.lang, output_type=pytesseract.Output.DICT)
        except Exception as exc:
            logger.warning("Tesseract OCR failed: %s", exc)
            return OCRResult(text="", engine_name="tesseract", warnings=[f"OCR failed: {exc}"])

        words, confidences = [], []
        for text, conf in zip(data.get("text", []), data.get("conf", [])):
            text = text.strip()
            if not text:
                continue
            words.append(text)
            try:
                conf_val = float(conf)
                if conf_val >= 0:
                    confidences.append(conf_val)
            except (TypeError, ValueError):
                pass

        mean_conf = sum(confidences) / len(confidences) if confidences else None
        return OCRResult(text=" ".join(words), mean_confidence=mean_conf, engine_name="tesseract")


_engine_singleton: Optional[BaseOCREngine] = None


def get_ocr_engine() -> BaseOCREngine:
    """Returns a shared OCR engine instance, auto-detecting availability."""
    global _engine_singleton
    if _engine_singleton is not None:
        return _engine_singleton

    tesseract_available = shutil.which("tesseract") is not None
    if tesseract_available:
        try:
            import pytesseract  # noqa: F401
            _engine_singleton = TesseractOCREngine()
            logger.info("OCR engine: Tesseract (found on PATH)")
        except ImportError:
            _engine_singleton = NullOCREngine()
            logger.warning("tesseract binary found but 'pytesseract' package not installed; OCR disabled")
    else:
        _engine_singleton = NullOCREngine()
        logger.warning("No tesseract binary found on PATH; OCR disabled (scanned pages/images won't be searchable)")

    return _engine_singleton
