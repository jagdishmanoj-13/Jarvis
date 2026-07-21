"""
language/language_generation_engine.py
=========================================

This IS the `LanguageGenerationEngine` the original project spec called
for: a swappable component that turns (retrieved facts + math results +
intent + conversation context) into a natural-sounding answer, with zero
pretrained LLM anywhere in the chain. Everything downstream of retrieval
(`core/qa_service.py`) talks to this module through `compose_answer()`
only — swapping this file for a future custom transformer later means
implementing the same function signature and changing one import in
`qa_service.py`, nothing else in the system.

Design decisions
-----------------
- The three ingredients are strictly separated, matching the spec's
  explicit requirement ("Knowledge Retrieval / Reasoning / Language
  Generation... separated"):
    1. Retrieval facts come in as already-scored hits (this module never
       searches anything itself).
    2. Grammar/communication assembly happens here, using
       `language.grammar_engine` (rules) and `language.communication_style`
       (templates) — no free-text generation, only slot-filling of
       curated, bundled phrasing around extracted facts.
    3. Math results, if any, come in pre-computed from
       `language.math_engine` — this module only phrases them.
- This is fundamentally an EXTRACTIVE + TEMPLATED system, not generative:
  the actual facts in the answer are always verbatim (or lightly
  truncated/normalized) text from the source document, wrapped in
  natural-sounding connective phrasing. It cannot say something the
  source documents didn't say, which is a deliberate, desirable property
  for an engineering/compliance assistant — no hallucination is possible
  because no free generation is happening.
- `low_confidence` propagates from OCR'd chunks (see
  `parser/image_parser.py`, `parser/pdf_parser.py`) through to a visible
  hedge phrase, so answers sourced from a shaky scan are never presented
  with the same confidence as a clean text extraction.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import List, Optional

from language import communication_style as style
from language.grammar_engine import (
    bullet_list, capitalize_first, ensure_sentence, join_list, normalize_whitespace,
    truncate_at_sentence_boundary,
)
from language.math_engine import MathEngineError
from reasoning.intent_detector import Intent

_MAX_FACT_CHARS = 500

_LEADING_QUESTION_RE = re.compile(
    r"^\s*(what is|what are|what does|what's|define|definition of|"
    r"how do i|how do you|how to|how can i|explain)\s+",
    re.IGNORECASE,
)
_LEADING_ARTICLE_RE = re.compile(r"^\s*(the|a|an)\s+", re.IGNORECASE)


_LEADING_FILENAME_RE = re.compile(r"^[a-z0-9_\-]+\.[a-z0-9]{1,5}\b")


def _smart_capitalize(text: str) -> str:
    """Like capitalize_first, but leaves a leading filename (e.g.
    'page.html states that...') uncapitalized, since 'Page.html' misreads
    as a proper-noun sentence start rather than the actual filename.
    """
    if _LEADING_FILENAME_RE.match(text):
        return text
    return capitalize_first(text)


def _extract_topic_phrase(question: str) -> str:
    """Strips leading interrogative phrasing and trailing punctuation from
    a question so it reads as a short noun phrase ("torque value for M8
    bolt") instead of dumping the entire raw question ("What is the torque
    value for M8 bolt?") into a sentence template — the latter reads as
    an obvious template artifact rather than natural phrasing.
    """
    text = question.strip().rstrip("?!.")
    text = _LEADING_QUESTION_RE.sub("", text)
    text = _LEADING_ARTICLE_RE.sub("", text)
    return text.strip() or question.strip().rstrip("?!.")


@dataclass
class Citation:
    filename: str
    location: Optional[str]  # e.g. "p.4" or "Torque Specifications > M8 Bolts"

    def render(self) -> str:
        return f"{self.filename}" + (f" ({self.location})" if self.location else "")


@dataclass
class ComposedAnswer:
    text: str
    citations: List[Citation] = field(default_factory=list)
    low_confidence: bool = False
    used_math_engine: bool = False


def _hit_location(hit: dict) -> Optional[str]:
    if hit.get("page_number"):
        return f"p.{hit['page_number']}"
    return hit.get("section_path")


def _is_low_confidence(hit: dict) -> bool:
    extra = hit.get("extra") or {}
    return bool(extra.get("low_confidence")) or hit.get("element_type") == "ocr_text"


def compose_answer(
    question: str,
    intent: Intent,
    hits: List[dict],
    session_id: str,
    resolved_topic: Optional[str] = None,
    math_result: Optional[str] = None,
    math_error: Optional[MathEngineError] = None,
    math_source: Optional[str] = None,  # "arithmetic" | "conversion" | "table" | None
) -> ComposedAnswer:
    seed_key = f"{session_id}:{question}"

    # --- Math-driven answers take priority when the question was clearly
    #     computational (Phase's math_engine already did the arithmetic;
    #     this function only phrases the result). ---
    if intent == Intent.CALCULATION and (math_result is not None or math_error is not None):
        return _compose_math_answer(question, math_result, math_error, hits, seed_key, math_source)

    if not hits:
        text = style.no_match_response(seed_key)
        return ComposedAnswer(text=text, citations=[])

    if intent == Intent.LIST or intent == Intent.PROCEDURE:
        return _compose_list_answer(question, intent, hits, seed_key)

    if intent == Intent.COMPARISON:
        return _compose_comparison_answer(question, hits, seed_key)

    return _compose_single_fact_answer(question, intent, hits, seed_key, resolved_topic)


def _compose_single_fact_answer(question, intent, hits, seed_key, resolved_topic) -> ComposedAnswer:
    top = hits[0]
    citation = Citation(filename=top["filename"], location=_hit_location(top))
    low_conf = _is_low_confidence(top)

    fact_text = normalize_whitespace(top["text"])
    fact_text = truncate_at_sentence_boundary(fact_text, _MAX_FACT_CHARS)
    # Avoid a stray mid-sentence capital when embedding an extracted
    # sentence after a connecting phrase ("...refers to The torque..."),
    # while leaving genuine acronyms/codes alone (M8, PPE) -- .isupper()
    # on the first two characters distinguishes "Th" (ordinary word,
    # lowercase it) from "M8"/"PP" (acronym/code, leave as-is).
    if fact_text and fact_text[0].isupper() and not fact_text[:2].isupper():
        fact_text = fact_text[0].lower() + fact_text[1:]

    opener = style.opener_for(intent.value, seed_key, source=top["filename"], topic=_extract_topic_phrase(question))
    sentence = ensure_sentence(f"{opener} {fact_text}")

    parts = [sentence]

    if low_conf:
        parts.insert(0, style.hedge_for_low_confidence(seed_key).strip())

    if resolved_topic:
        parts.insert(0, style.follow_up_acknowledgement(resolved_topic[:60], seed_key))

    if len(hits) > 1:
        parts.append(style.multi_result_note(len(hits) - 1, seed_key))

    closer = style.closer_for(seed_key)
    if closer:
        parts.append(closer)

    text = " ".join(p for p in parts if p)
    citations = [Citation(filename=h["filename"], location=_hit_location(h)) for h in hits[:5]]
    return ComposedAnswer(text=text, citations=citations, low_confidence=low_conf)


def _compose_list_answer(question, intent, hits, seed_key) -> ComposedAnswer:
    top_source = hits[0]["filename"]
    opener = style.opener_for(intent.value, seed_key, source=top_source, topic=_extract_topic_phrase(question))
    opener = opener.rstrip(",.: ")  # these openers are written to lead into a sentence, not a list header
    items = []
    seen_texts = set()
    for h in hits[:8]:
        t = normalize_whitespace(h["text"])
        t = truncate_at_sentence_boundary(t, 220)
        if t not in seen_texts:
            items.append(t)
            seen_texts.add(t)

    body = bullet_list(items)
    text = f"{_smart_capitalize(opener)}:\n\n{body}"
    closer = style.closer_for(seed_key)
    if closer:
        text += f"\n\n{closer}"

    citations = [Citation(filename=h["filename"], location=_hit_location(h)) for h in hits[:8]]
    low_conf = any(_is_low_confidence(h) for h in hits[:8])
    return ComposedAnswer(text=text, citations=citations, low_confidence=low_conf)


def _compose_comparison_answer(question, hits, seed_key) -> ComposedAnswer:
    # Group by source document so the comparison is "per source", not per chunk.
    by_doc: dict = {}
    for h in hits:
        by_doc.setdefault(h["filename"], []).append(h)

    opener = style.opener_for(Intent.COMPARISON.value, seed_key, source=join_list(list(by_doc.keys())[:3]))
    opener = opener.rstrip(",.: ")
    lines = []
    for filename, doc_hits in list(by_doc.items())[:4]:
        snippet = truncate_at_sentence_boundary(normalize_whitespace(doc_hits[0]["text"]), 200)
        lines.append(f"{filename}: {snippet}")

    text = f"{_smart_capitalize(opener)}:\n\n" + bullet_list(lines)
    citations = [Citation(filename=h["filename"], location=_hit_location(h)) for h in hits[:6]]
    return ComposedAnswer(text=text, citations=citations)


def _compose_math_answer(question, math_result, math_error, hits, seed_key, math_source) -> ComposedAnswer:
    if math_error is not None:
        text = (
            f"I tried to compute that but ran into an issue: {math_error}. "
            f"If this relates to values in a specific document, try asking about the value directly "
            f"and I'll pull it from there instead."
        )
        citations = [Citation(filename=h["filename"], location=_hit_location(h)) for h in hits[:3]]
        return ComposedAnswer(text=text, citations=citations, used_math_engine=True)

    if math_source in ("arithmetic", "conversion"):
        # Self-contained math: doesn't depend on any document, so it must
        # NOT be attributed to whatever unrelated passage the keyword
        # search happened to return for the question text -- that would
        # misrepresent an ordinary calculator result as sourced fact.
        text = ensure_sentence(f"That works out to {math_result}")
        return ComposedAnswer(text=text, citations=[], used_math_engine=True)

    # math_source == "table": genuinely sourced from a specific retrieved
    # table, so cite it. Use a bare connector (no dangling "the value is"
    # verb) since math_result is already a complete clause ("the mean of
    # X is 25...") -- concatenating a verb-ending opener in front of that
    # would double up the predicate ("the value is the mean ... is 25").
    source = hits[0]["filename"] if hits else "the indexed documents"
    opener = f"According to {source},"
    text = ensure_sentence(f"{opener} {math_result}")
    citations = [Citation(filename=h["filename"], location=_hit_location(h)) for h in hits[:3]]
    return ComposedAnswer(text=text, citations=citations, used_math_engine=True)
