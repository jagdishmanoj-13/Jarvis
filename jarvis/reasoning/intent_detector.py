"""
reasoning/intent_detector.py
==============================

Classifies a question into one of a fixed set of intents using regex/
keyword pattern rules — no classifier model, no embeddings, no LLM.

Design decisions
-----------------
- Intent categories are chosen to match what `language.communication_style`
  and `core.qa_service` actually branch on: DEFINITION, PROCEDURE,
  NUMERIC_SPEC, CALCULATION, COMPARISON, LIST, FOLLOW_UP, GENERAL. Adding
  a new intent means adding one pattern group here and one template bank
  entry in communication_style.py — the two are deliberately kept in
  lock-step vocabulary.
- Rules are checked in a specific priority order (most specific first):
  CALCULATION and FOLLOW_UP are checked before the more general categories,
  since e.g. "what is the average torque" would otherwise match both
  NUMERIC_SPEC ("what is") and CALCULATION ("average") — the ordering
  encodes which reading should win.
- This is a *heuristic* classifier, not a guarantee — ambiguous or oddly
  phrased questions may get GENERAL as a safe fallback, and
  `core.qa_service` always still runs retrieval regardless of intent, so a
  misclassified intent affects phrasing/emphasis, not whether an answer is
  found at all.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum

from language.math_engine import looks_like_math_question


class Intent(str, Enum):
    DEFINITION = "definition"
    PROCEDURE = "procedure"
    NUMERIC_SPEC = "numeric_spec"
    CALCULATION = "calculation"
    COMPARISON = "comparison"
    LIST = "list"
    FOLLOW_UP = "follow_up"
    GENERAL = "general"


@dataclass
class IntentResult:
    intent: Intent
    confidence: float  # 0-1, heuristic strength of the pattern match, not a probability
    matched_rule: str


_DEFINITION_RE = re.compile(r"^\s*(what is|what are|define|definition of|what does .* mean)\b", re.IGNORECASE)
_PROCEDURE_RE = re.compile(r"^\s*(how (do|to|can)|what are the steps|procedure for|process for)\b", re.IGNORECASE)
_NUMERIC_SPEC_RE = re.compile(
    r"\b(torque|value|spec(ification)?|rating|tolerance|dimension|pressure|temperature|weight|size|voltage|current)\b",
    re.IGNORECASE,
)
_COMPARISON_RE = re.compile(r"\b(compare|versus|vs\.?|difference between|which is (better|higher|lower))\b", re.IGNORECASE)
_LIST_RE = re.compile(r"^\s*(list|what are all|show me all|enumerate)\b", re.IGNORECASE)
_FOLLOW_UP_RE = re.compile(
    r"^\s*(and|what about|what if|also|then|next|another|more on this|continue)\b|^\s*(it|that|this|those|these)\b",
    re.IGNORECASE,
)
_SHORT_FOLLOW_UP_WORD_COUNT = 4  # very short queries with no clear subject often lean on prior context


def detect_intent(question: str, has_prior_turn: bool = False) -> IntentResult:
    q = question.strip()
    if not q:
        return IntentResult(Intent.GENERAL, 0.0, "empty_question")

    if looks_like_math_question(q):
        return IntentResult(Intent.CALCULATION, 0.85, "math_trigger_words")

    # Explicit follow-up phrasing ("what about...", "and...", "it/that...")
    # is checked before the other specific-intent patterns, since it's an
    # unambiguous signal regardless of what follows.
    if has_prior_turn and _FOLLOW_UP_RE.search(q):
        return IntentResult(Intent.FOLLOW_UP, 0.6, "follow_up_pattern")

    if _COMPARISON_RE.search(q):
        return IntentResult(Intent.COMPARISON, 0.8, "comparison_keywords")

    if _LIST_RE.search(q):
        return IntentResult(Intent.LIST, 0.75, "list_keywords")

    if _PROCEDURE_RE.search(q):
        return IntentResult(Intent.PROCEDURE, 0.8, "procedure_pattern")

    if _DEFINITION_RE.search(q):
        return IntentResult(Intent.DEFINITION, 0.75, "definition_pattern")

    if _NUMERIC_SPEC_RE.search(q):
        return IntentResult(Intent.NUMERIC_SPEC, 0.6, "numeric_spec_keywords")

    # Weak fallback: a very short query with no other identifiable pattern
    # often leans on prior context ("Torque for M10?" after a torque
    # question) -- but only once every more specific pattern has had a
    # chance to match, so it never overrides a clear signal like "list...".
    if has_prior_turn and len(q.split()) <= _SHORT_FOLLOW_UP_WORD_COUNT:
        return IntentResult(Intent.FOLLOW_UP, 0.4, "short_query_fallback")

    return IntentResult(Intent.GENERAL, 0.3, "fallback")
