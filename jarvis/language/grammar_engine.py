"""
language/grammar_engine.py
============================

Deterministic, rule-based sentence-construction helpers. This is the
"grammar" layer of the language-generation stack: it never generates
content, only assembles/formats content that other modules (the retrieval
layer, the math engine) already produced, according to standard English
grammar rules.

Design decisions
-----------------
- Every function here is a pure function (text/data in, text out) with no
  hidden state and no model weights — this is what "no LLM anywhere" means
  concretely: there is no learned component in this module at all, just
  codified rules a style guide would state explicitly (Oxford comma usage,
  sentence-initial capitalization, etc.).
- `join_list()` implements the standard "A, B, and C" / "A and B" / "A"
  rules including the Oxford comma, since raw joined fact lists ("bolt M6,
  bolt M8, bolt M10") read as a data dump, not a sentence, without this.
- `format_number()` renders large numbers with thousands separators and
  trims trailing zeros from decimals (25.0 -> "25", 3.140 -> "3.14"),
  because raw floats from parsed spreadsheet/table cells look computer-
  generated rather than human-written otherwise.
- `ensure_sentence()` guarantees every composed sentence starts with a
  capital letter and ends with terminal punctuation, which matters a lot
  for the overall system feeling like it's "talking" rather than "dumping
  extracted text", especially since extracted chunk text from a table cell
  or bullet point often has neither.
"""

from __future__ import annotations

import re
from typing import List, Sequence


def capitalize_first(text: str) -> str:
    if not text:
        return text
    return text[0].upper() + text[1:]


def ensure_terminal_punctuation(text: str) -> str:
    text = text.rstrip()
    if not text:
        return text
    if text[-1] not in ".!?:":
        return text + "."
    return text


def ensure_sentence(text: str) -> str:
    text = text.strip()
    if not text:
        return text
    return ensure_terminal_punctuation(capitalize_first(text))


def join_list(items: Sequence[str], conjunction: str = "and", oxford_comma: bool = True) -> str:
    items = [str(i).strip() for i in items if str(i).strip()]
    if not items:
        return ""
    if len(items) == 1:
        return items[0]
    if len(items) == 2:
        return f"{items[0]} {conjunction} {items[1]}"
    head = ", ".join(items[:-1])
    sep = "," if oxford_comma else ""
    return f"{head}{sep} {conjunction} {items[-1]}"


def format_number(value) -> str:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return str(value)
    if f == int(f):
        return f"{int(f):,}"
    formatted = f"{f:,.4f}".rstrip("0").rstrip(".")
    return formatted


_WHITESPACE_RE = re.compile(r"[ \t]+")
_MULTI_NEWLINE_RE = re.compile(r"\n{3,}")
_SPACE_BEFORE_PUNCT_RE = re.compile(r"\s+([.,!?;:])")


def normalize_whitespace(text: str) -> str:
    text = _WHITESPACE_RE.sub(" ", text)
    text = _MULTI_NEWLINE_RE.sub("\n\n", text)
    text = _SPACE_BEFORE_PUNCT_RE.sub(r"\1", text)
    return text.strip()


def truncate_at_sentence_boundary(text: str, max_chars: int) -> str:
    """Truncates long extracted text at the nearest sentence end at/before
    max_chars, rather than mid-word/mid-sentence, so a shortened passage
    still reads as a complete thought.
    """
    text = text.strip()
    if len(text) <= max_chars:
        return text
    window = text[:max_chars]
    boundary = max(window.rfind(". "), window.rfind("! "), window.rfind("? "))
    if boundary > max_chars * 0.4:  # only trust the boundary if it's not absurdly early
        return window[:boundary + 1].strip()
    return window.rstrip() + "..."


def bullet_list(items: Sequence[str]) -> str:
    return "\n".join(f"- {ensure_sentence(str(i))}" for i in items if str(i).strip())


def title_case_heading(text: str) -> str:
    minor_words = {"a", "an", "the", "of", "in", "on", "for", "and", "or", "to", "with"}
    words = text.split()
    result = []
    for i, w in enumerate(words):
        if i > 0 and w.lower() in minor_words:
            result.append(w.lower())
        else:
            result.append(w[:1].upper() + w[1:] if w else w)
    return " ".join(result)
