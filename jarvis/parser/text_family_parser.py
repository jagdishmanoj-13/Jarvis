"""
parser/text_family_parser.py
=============================

Parsers for every format that is fundamentally "text with some structure",
implemented on the standard library plus lightweight, already-required
dependencies (`pyyaml`, `beautifulsoup4`) rather than heavy format-specific
SDKs. Grouped in one module because they share a lot of plumbing (encoding
detection, line-based section tracking).

Design decisions
-----------------
- `_read_text_with_encoding_detection` uses `chardet` when available and
  falls back to a UTF-8 -> latin-1 attempt chain otherwise. Engineering
  document stores commonly contain files saved by legacy Windows tools in
  cp1252 or latin-1, not just UTF-8, so guessing wrong would silently
  corrupt extracted text (a real problem for search/citation quality).
- Markdown headings (`#`, `##`, ...) and simple code comment blocks are
  used to build a `section_path` breadcrumb (e.g. "Setup > Installation"),
  which is what lets the citation engine say *where* in a long file an
  answer came from, not just which file.
- `HtmlParser` uses BeautifulSoup (already a dependency) to strip
  boilerplate (script/style tags) and to preserve heading structure; it
  purposefully avoids rendering-heavy scraping frameworks.
- `XmlParser` uses the stdlib `xml.etree.ElementTree`, with
  `resolve_entities` left at its safe default and no custom entity
  resolution, to avoid XXE risk when parsing files from an arbitrary
  network share.
- `RtfParser` has NO external dependency (Citrix: no internet to `pip
  install striprtf`). It implements a conservative regex-based RTF control
  sequence stripper. This is not a complete RTF interpreter (RTF is a
  complex format), but it recovers plain text adequately for typical
  engineering SOP/memo documents and is honest about that limitation via
  `parser_warnings`.
"""

from __future__ import annotations

import configparser
import json
import re
from pathlib import Path
from typing import List

import yaml
from bs4 import BeautifulSoup

from models.document import ExtractionElementType
from parser.base_parser import BaseParser, ParsedDocument, ParserError, RawElement
from utils.logger import get_logger

logger = get_logger(__name__)


def _read_text_with_encoding_detection(path: Path) -> str:
    raw = path.read_bytes()
    if not raw:
        return ""
    try:
        import chardet
        detected = chardet.detect(raw[:200_000])  # sample; full-file detect is slow on huge logs
        encoding = detected.get("encoding") or "utf-8"
        confidence = detected.get("confidence") or 0
        if confidence < 0.5:
            encoding = "utf-8"
    except ImportError:
        encoding = "utf-8"

    for enc in (encoding, "utf-8", "cp1252", "latin-1"):
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", errors="replace")


# ----------------------------------------------------------------------
# Plain text (.txt, .log)
# ----------------------------------------------------------------------
class PlainTextParser(BaseParser):
    @classmethod
    def supported_extensions(cls) -> List[str]:
        return [".txt", ".log"]

    def parse(self, path: Path) -> ParsedDocument:
        text = _read_text_with_encoding_detection(path)
        element_type = ExtractionElementType.BODY_TEXT
        doc = ParsedDocument(title=path.stem, page_count=1)
        # .log files benefit from being treated as one big body chunk rather
        # than heading-split, since logs rarely have meaningful headings.
        doc.elements.append(RawElement(text=text, element_type=element_type, order_index=0))
        return doc


# ----------------------------------------------------------------------
# Markdown (.md)
# ----------------------------------------------------------------------
class MarkdownParser(BaseParser):
    _HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")

    @classmethod
    def supported_extensions(cls) -> List[str]:
        return [".md"]

    def parse(self, path: Path) -> ParsedDocument:
        text = _read_text_with_encoding_detection(path)
        doc = ParsedDocument(title=path.stem, page_count=1)
        section_stack: List[str] = []
        buffer: List[str] = []
        order = 0

        def flush():
            nonlocal order
            body = "\n".join(buffer).strip()
            if body:
                doc.elements.append(RawElement(
                    text=body, element_type=ExtractionElementType.BODY_TEXT,
                    section_path=" > ".join(section_stack) if section_stack else None,
                    order_index=order,
                ))
                order += 1
            buffer.clear()

        for line in text.splitlines():
            match = self._HEADING_RE.match(line)
            if match:
                flush()
                level, title = len(match.group(1)), match.group(2).strip()
                section_stack = section_stack[:level - 1] + [title]
                doc.elements.append(RawElement(
                    text=title, element_type=ExtractionElementType.HEADING,
                    section_path=" > ".join(section_stack), order_index=order,
                ))
                order += 1
                if doc.title == path.stem and level == 1:
                    doc.title = title
            else:
                buffer.append(line)
        flush()
        return doc


# ----------------------------------------------------------------------
# Source code (.py, .java, .cs, .cpp, .c, .h, .sql)
# ----------------------------------------------------------------------
class CodeParser(BaseParser):
    """Code is indexed as searchable text with light structural hints
    (function/class signature lines flagged as HEADING) so engineers can
    ask things like "where is the torque calculation implemented" and get
    a pointer to the right function, not just "somewhere in this file".
    """
    _SIGNATURE_PATTERNS = {
        ".py": re.compile(r"^\s*(def|class)\s+\w+"),
        ".java": re.compile(r"^\s*(public|private|protected).*\b(class|interface|void|\w+\()"),
        ".cs": re.compile(r"^\s*(public|private|protected|internal).*\b(class|interface|void|\w+\()"),
        ".cpp": re.compile(r"^\s*\w[\w:<>~]*\s+\w+\s*\([^;]*\)\s*\{?$"),
        ".c": re.compile(r"^\s*\w[\w\*]*\s+\w+\s*\([^;]*\)\s*\{?$"),
        ".h": re.compile(r"^\s*\w[\w\*]*\s+\w+\s*\([^;]*\)\s*;?$"),
        ".sql": re.compile(r"^\s*(CREATE|ALTER|DROP)\s+(TABLE|VIEW|PROCEDURE|FUNCTION)", re.IGNORECASE),
    }

    @classmethod
    def supported_extensions(cls) -> List[str]:
        return [".py", ".java", ".cs", ".cpp", ".c", ".h", ".sql"]

    def parse(self, path: Path) -> ParsedDocument:
        text = _read_text_with_encoding_detection(path)
        pattern = self._SIGNATURE_PATTERNS.get(path.suffix.lower())
        doc = ParsedDocument(title=path.name, page_count=1)

        if not pattern:
            doc.elements.append(RawElement(text=text, element_type=ExtractionElementType.BODY_TEXT))
            return doc

        lines = text.splitlines()
        current_section = None
        buffer: List[str] = []
        order = 0

        def flush():
            nonlocal order
            body = "\n".join(buffer).strip()
            if body:
                doc.elements.append(RawElement(
                    text=body, element_type=ExtractionElementType.BODY_TEXT,
                    section_path=current_section, order_index=order,
                ))
                order += 1
            buffer.clear()

        for line in lines:
            if pattern.match(line):
                flush()
                current_section = line.strip()[:120]
                doc.elements.append(RawElement(
                    text=current_section, element_type=ExtractionElementType.HEADING,
                    section_path=current_section, order_index=order,
                ))
                order += 1
            buffer.append(line)
        flush()
        return doc


# ----------------------------------------------------------------------
# Structured text: JSON, YAML, INI (also covers .yml)
# ----------------------------------------------------------------------
class StructuredTextParser(BaseParser):
    """These formats are mostly config/data, not prose. We pretty-print them
    back to readable text (preserving key nesting) so they remain fully
    keyword-searchable, and store each top-level key as its own element so
    a search hit can cite "server.timeout_ms in config.yaml" precisely.
    """

    @classmethod
    def supported_extensions(cls) -> List[str]:
        return [".json", ".yaml", ".yml", ".ini"]

    def parse(self, path: Path) -> ParsedDocument:
        ext = path.suffix.lower()
        text = _read_text_with_encoding_detection(path)
        doc = ParsedDocument(title=path.name, page_count=1)

        try:
            if ext == ".json":
                data = json.loads(text)
                self._emit_mapping(doc, data)
            elif ext in (".yaml", ".yml"):
                data = yaml.safe_load(text)
                self._emit_mapping(doc, data)
            elif ext == ".ini":
                parser = configparser.ConfigParser()
                parser.read_string(text)
                order = 0
                for section in parser.sections():
                    items = "\n".join(f"{k} = {v}" for k, v in parser.items(section))
                    doc.elements.append(RawElement(
                        text=f"[{section}]\n{items}", element_type=ExtractionElementType.BODY_TEXT,
                        section_path=section, order_index=order,
                    ))
                    order += 1
        except (json.JSONDecodeError, yaml.YAMLError, configparser.Error) as exc:
            doc.parser_warnings.append(f"Structured parse failed, falling back to raw text: {exc}")
            doc.elements.append(RawElement(text=text, element_type=ExtractionElementType.BODY_TEXT))

        if not doc.elements:
            doc.elements.append(RawElement(text=text, element_type=ExtractionElementType.BODY_TEXT))
        return doc

    @staticmethod
    def _emit_mapping(doc: ParsedDocument, data) -> None:
        if isinstance(data, dict):
            for order, (key, value) in enumerate(data.items()):
                rendered = json.dumps(value, indent=2, ensure_ascii=False, default=str)
                doc.elements.append(RawElement(
                    text=f"{key}:\n{rendered}", element_type=ExtractionElementType.BODY_TEXT,
                    section_path=str(key), order_index=order,
                ))
        else:
            doc.elements.append(RawElement(
                text=json.dumps(data, indent=2, ensure_ascii=False, default=str),
                element_type=ExtractionElementType.BODY_TEXT,
            ))


# ----------------------------------------------------------------------
# HTML
# ----------------------------------------------------------------------
class HtmlParser(BaseParser):
    @classmethod
    def supported_extensions(cls) -> List[str]:
        return [".html", ".htm"]

    def parse(self, path: Path) -> ParsedDocument:
        raw_html = _read_text_with_encoding_detection(path)
        soup = BeautifulSoup(raw_html, "html.parser")

        for tag in soup(["script", "style", "noscript"]):
            tag.decompose()

        doc = ParsedDocument(title=(soup.title.string.strip() if soup.title and soup.title.string else path.stem))
        order = 0
        current_section = None

        for el in soup.find_all(["h1", "h2", "h3", "h4", "h5", "h6", "p", "li", "table", "caption", "a"]):
            text = el.get_text(strip=True, separator=" ")
            if not text:
                continue
            if el.name.startswith("h") and el.name[1:].isdigit():
                current_section = text
                doc.elements.append(RawElement(text=text, element_type=ExtractionElementType.HEADING,
                                                section_path=current_section, order_index=order))
            elif el.name == "table":
                doc.elements.append(RawElement(text=text, element_type=ExtractionElementType.TABLE,
                                                section_path=current_section, order_index=order))
            elif el.name == "caption":
                doc.elements.append(RawElement(text=text, element_type=ExtractionElementType.CAPTION,
                                                section_path=current_section, order_index=order))
            elif el.name == "a" and el.get("href"):
                doc.elements.append(RawElement(text=text, element_type=ExtractionElementType.HYPERLINK,
                                                section_path=current_section, order_index=order,
                                                extra={"href": el.get("href")}))
                continue  # don't double-count link text as body text
            else:
                doc.elements.append(RawElement(text=text, element_type=ExtractionElementType.BODY_TEXT,
                                                section_path=current_section, order_index=order))
            order += 1
        return doc


# ----------------------------------------------------------------------
# XML
# ----------------------------------------------------------------------
class XmlParser(BaseParser):
    @classmethod
    def supported_extensions(cls) -> List[str]:
        return [".xml"]

    def parse(self, path: Path) -> ParsedDocument:
        import xml.etree.ElementTree as ET

        doc = ParsedDocument(title=path.stem)
        try:
            tree = ET.parse(str(path))
        except ET.ParseError as exc:
            raise ParserError(f"Malformed XML in {path}: {exc}")

        order = 0

        def walk(element, path_stack):
            nonlocal order
            tag = element.tag.split("}")[-1]  # strip XML namespace
            new_stack = path_stack + [tag]
            text = (element.text or "").strip()
            if text:
                doc.elements.append(RawElement(
                    text=text, element_type=ExtractionElementType.BODY_TEXT,
                    section_path=" > ".join(new_stack), order_index=order,
                ))
                order += 1
            for attr_key, attr_val in element.attrib.items():
                doc.elements.append(RawElement(
                    text=f"{attr_key}={attr_val}", element_type=ExtractionElementType.METADATA,
                    section_path=" > ".join(new_stack), order_index=order,
                ))
                order += 1
            for child in element:
                walk(child, new_stack)

        walk(tree.getroot(), [])
        return doc


# ----------------------------------------------------------------------
# RTF (dependency-free approximation)
# ----------------------------------------------------------------------
class RtfParser(BaseParser):
    """Minimal RTF-to-text conversion using only the standard library.

    Not a full RTF interpreter — handles the common control-word / group
    syntax well enough for typical memos and SOPs exported from Word.
    Anything it can't confidently strip is left in `parser_warnings`
    rather than silently mangling text.
    """
    _CONTROL_WORD_RE = re.compile(r"\\[a-zA-Z]+-?\d* ?")
    _HEX_ESCAPE_RE = re.compile(r"\\'[0-9a-fA-F]{2}")

    @classmethod
    def supported_extensions(cls) -> List[str]:
        return [".rtf"]

    def parse(self, path: Path) -> ParsedDocument:
        raw = _read_text_with_encoding_detection(path)
        doc = ParsedDocument(title=path.stem, page_count=1)
        doc.parser_warnings.append(
            "RTF parsed with a lightweight built-in stripper (no external RTF "
            "library available offline); complex formatting/tables may not "
            "extract perfectly."
        )
        text = self._HEX_ESCAPE_RE.sub(" ", raw)
        text = self._CONTROL_WORD_RE.sub(" ", text)
        text = re.sub(r"[{}]", "", text)
        text = re.sub(r"\s+", " ", text).strip()
        doc.elements.append(RawElement(text=text, element_type=ExtractionElementType.BODY_TEXT))
        return doc
