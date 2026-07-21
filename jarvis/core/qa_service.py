"""
core/qa_service.py
====================

The single entry point the UI calls to ask JARVIS a question. Wires
together every reasoning/language module built so far, in the order the
spec's architecture diagram implies: Context -> Intent -> Retrieval ->
Math (if applicable) -> Language Generation -> Memory write-back.

Design decisions
-----------------
- `answer_question()` is the ONLY function `ui/app.py` needs to call for
  the chat experience — this keeps the UI layer from having to know about
  intent detection, context resolution, or the math engine individually,
  matching the "swap one module without touching others" architecture
  goal.
- Math questions are handled specially: if `math_engine.looks_like_math_question`
  fires, we first try `evaluate_arithmetic()` directly on the question
  (covers "what is 25 * 1.2"); if that fails (e.g. the question references
  a value that has to come FROM a document, like "what's the average
  torque in the spec table"), we fall back to running retrieval first,
  then computing `table_statistics()` over whichever retrieved chunk is a
  TABLE, and only give up with a math_error if neither path produces a
  number.
- Every turn (user question AND composed answer) is written to
  conversation memory via `MetadataStore`, including `active_topic` /
  `active_document_id`, which is exactly what `memory.context_manager`
  reads back on the next turn — this is the actual mechanism behind
  "remembers previous conversations... allows follow-up questions".
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List, Optional

from database.metadata_store import MetadataStore
from language.language_generation_engine import ComposedAnswer, compose_answer
from language.math_engine import (
    MathEngineError, convert_units, evaluate_arithmetic, looks_like_math_question, table_statistics,
)
from memory.context_manager import resolve_query
from models.document import ConversationTurn
from reasoning.intent_detector import Intent, detect_intent
from retrieval.hybrid_search import search_chunks_compat as search_chunks
from utils.logger import get_logger

logger = get_logger(__name__)

_BARE_ARITHMETIC_RE = re.compile(r"^[\d\s\.\+\-\*/×÷\(\)]+$")
_CONVERSION_RE = re.compile(
    r"(-?\d+(?:\.\d+)?)\s*([a-zA-Z°\-]+)\s*(?:to|in|into)\s*([a-zA-Z°\-]+)", re.IGNORECASE
)


@dataclass
class QAResponse:
    answer: ComposedAnswer
    intent: Intent
    search_query_used: str
    hit_count: int


def _try_direct_arithmetic(question: str) -> Optional[str]:
    """Handles 'what is 25 * 1.2' style questions with no document lookup
    needed at all.
    """
    candidate = re.sub(r"(?i)^(what is|calculate|compute)\s+", "", question).strip().rstrip("?")
    if _BARE_ARITHMETIC_RE.match(candidate.replace(" ", "")):
        try:
            return evaluate_arithmetic(candidate)
        except MathEngineError:
            return None
    return None


def _try_unit_conversion(question: str) -> Optional[str]:
    """Handles 'convert 25 Nm to lb-ft' / '25 Nm in lb-ft' style questions."""
    match = _CONVERSION_RE.search(question)
    if not match:
        return None
    value_str, from_unit, to_unit = match.groups()
    try:
        value = float(value_str)
        result = convert_units(value, from_unit, to_unit)
    except (MathEngineError, ValueError):
        return None
    rounded = round(result, 4) if result != int(result) else int(result)
    return f"{rounded} {to_unit} (converted from {value} {from_unit})"


def _try_table_stat_from_hits(question: str, hits: List[dict]) -> Optional[tuple]:
    """Returns (stat_sentence, source_hit) so the caller can put the hit
    that the number actually came from first for citation purposes --
    without this, the composed answer could cite a different, unrelated
    document than the one the statistic was computed from.
    """
    q = question.lower()
    stat_key = "mean" if any(w in q for w in ("average", "mean")) else \
        "max" if any(w in q for w in ("maximum", "max ")) else \
        "min" if any(w in q for w in ("minimum", "min ")) else \
        "sum" if any(w in q for w in ("sum", "total")) else "mean"

    for h in hits:
        if h.get("element_type") != "table":
            continue
        stats = table_statistics(h["text"])
        if stats:
            value = stats[stat_key]
            rounded = round(value, 4) if value != int(value) else int(value)
            sentence = f"the {stat_key} of {stats['column_name']} is {rounded} (from {stats['count']} value(s))"
            return sentence, h
    return None


def answer_question(store: MetadataStore, session_id: str, question: str) -> QAResponse:
    question = question.strip()

    prior_turn_exists = store.get_recent_turns(session_id, limit=1) != []
    intent_result = detect_intent(question, has_prior_turn=prior_turn_exists)
    intent = intent_result.intent

    is_follow_up = intent == Intent.FOLLOW_UP
    resolved = resolve_query(store, session_id, question, is_follow_up=is_follow_up)
    search_query = resolved.search_query

    hits = search_chunks(store, search_query, limit=8)
    if is_follow_up and not hits:
        # Follow-up merge sometimes over-constrains the query; retry with
        # just the new question's own terms if the merged query found nothing.
        hits = search_chunks(store, question, limit=8)
        search_query = question

    math_result, math_error, math_source = None, None, None
    if intent == Intent.CALCULATION:
        direct = _try_direct_arithmetic(question)
        conversion = _try_unit_conversion(question) if direct is None else None
        if direct is not None:
            math_result, math_source = direct, "arithmetic"
        elif conversion is not None:
            math_result, math_source = conversion, "conversion"
        else:
            table_stat = _try_table_stat_from_hits(question, hits)
            if table_stat is not None:
                math_result, source_hit = table_stat
                math_source = "table"
                # Put the hit the number was actually computed from first,
                # so the composed answer's opener/citation names the right
                # source document instead of whichever hit happened to
                # rank first in the unrelated keyword search.
                hits = [source_hit] + [h for h in hits if h is not source_hit]
            else:
                math_error = MathEngineError(
                    "I couldn't find a computable number for this — either a table with the right "
                    "values isn't indexed yet, or the question needs a value I can't identify."
                )

    composed = compose_answer(
        question=question, intent=intent, hits=hits, session_id=session_id,
        resolved_topic=resolved.active_topic if resolved.used_follow_up_resolution else None,
        math_result=math_result, math_error=math_error, math_source=math_source,
    )

    store.add_conversation_turn(ConversationTurn(
        session_id=session_id, role="user", content=question,
        active_document_id=resolved.active_document_id,
    ))
    store.add_conversation_turn(ConversationTurn(
        session_id=session_id, role="assistant", content=composed.text,
        active_topic=question,
        active_document_id=hits[0]["document_id"] if hits else resolved.active_document_id,
        cited_chunk_ids=[],
    ))

    logger.info("Q&A: intent=%s query=%r hits=%d", intent.value, search_query, len(hits))
    return QAResponse(answer=composed, intent=intent, search_query_used=search_query, hit_count=len(hits))
