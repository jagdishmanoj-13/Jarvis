"""
memory/context_manager.py
============================

Resolves follow-up questions ("what about the next one?", "and that
component?") into a search-ready query using the conversation memory
already stored in `MetadataStore`. Rule-based, not learned: it looks at
the last topic/document from `ConversationTurn` rows and merges it with
the new question.

Design decisions
-----------------
- Follow-up resolution here is deliberately simple: prepend the previous
  question's key terms to the new (usually short/pronoun-laden) question
  before it goes to search, e.g. "torque value M8 bolt" + "what about
  M10?" -> "torque value M10 bolt what about". This is crude compared to
  true coreference resolution, but it is transparent, debuggable, and
  correct far more often than leaving a 3-word query ("what about M10")
  to fend for itself against a keyword index — and it costs nothing to
  compute (no model, just string concatenation with light cleanup).
- `ContextState` is reconstructed fresh from the database on each call
  rather than kept purely in memory, so context correctly survives a
  Streamlit rerun/app restart — conversation memory is explicitly a
  spec requirement ("remember previous conversations... across
  restarts"), not just an in-process nicety.
- Only the most recent USER turn is used for merging (not the assistant's
  answer text), since merging in the assistant's prior answer tends to
  inject unrelated vocabulary from whatever passage was retrieved rather
  than the user's actual topic thread.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List, Optional

from database.metadata_store import MetadataStore
from models.document import ConversationTurn

_STOPWORDS = {
    "what", "is", "the", "a", "an", "are", "was", "were", "do", "does", "did",
    "how", "why", "when", "where", "which", "who", "and", "or", "of", "for",
    "to", "in", "on", "at", "about", "this", "that", "it", "with",
}


@dataclass
class ResolvedQuery:
    search_query: str
    active_topic: Optional[str]
    active_document_id: Optional[str]
    used_follow_up_resolution: bool


def _key_terms(text: str, max_terms: int = 6) -> List[str]:
    words = re.findall(r"[A-Za-z0-9][A-Za-z0-9\-]*", text.lower())
    terms = [w for w in words if w not in _STOPWORDS and len(w) > 1]
    return terms[:max_terms]


def get_last_turn_context(store: MetadataStore, session_id: str) -> Optional[ConversationTurn]:
    recent = store.get_recent_turns(session_id, limit=10)
    for turn in reversed(recent):
        if turn.role == "user":
            return turn
    return None


def resolve_query(store: MetadataStore, session_id: str, question: str, is_follow_up: bool) -> ResolvedQuery:
    if not is_follow_up:
        return ResolvedQuery(search_query=question, active_topic=None, active_document_id=None,
                              used_follow_up_resolution=False)

    last_turn = get_last_turn_context(store, session_id)
    if last_turn is None:
        return ResolvedQuery(search_query=question, active_topic=None, active_document_id=None,
                              used_follow_up_resolution=False)

    prior_terms = _key_terms(last_turn.content)
    new_terms = _key_terms(question)
    # New question's own terms take priority (they're what changed), prior
    # context terms are appended to keep the thread without drowning it out.
    merged_terms = new_terms + [t for t in prior_terms if t not in new_terms]
    merged_query = " ".join(merged_terms) if merged_terms else question

    return ResolvedQuery(
        search_query=merged_query, active_topic=last_turn.content,
        active_document_id=last_turn.active_document_id, used_follow_up_resolution=True,
    )
