"""
language/communication_style.py
=================================

The "communication skill" knowledge base: a hand-curated bank of natural
phrasing templates for how a human assistant actually talks — opening an
answer, introducing a source, hedging uncertainty, offering to elaborate,
saying "I don't know" gracefully, and so on.

Design decisions
-----------------
- This is data, not a model: a categorized dict of phrase templates,
  authored once and bundled with the code (the "database... kept in the
  backend" the spec asks for). No network fetch, no training, no
  probability distribution over a vocabulary — just curated English
  phrasing choices a technical writer would sanction.
- Multiple variants per category, chosen with a *session-seeded*
  pseudo-random selection (not pure random) so:
    (a) responses don't feel robotically identical every time ("Based on
        X..." verbatim for every single answer), which is what makes
        template-based systems feel like template-based systems, and
    (b) within one conversation the voice stays internally consistent
        rather than randomly switching tone turn to turn.
- Categories map directly onto `reasoning.intent_detector`'s intent
  labels, so `language.language_generation_engine` can look up the right
  bucket by intent without any string matching of its own.
- Deliberately avoids overclaiming ("I'm confident that...", "I know
  that...") for anything sourced from a single low-confidence OCR chunk —
  see `HEDGE_LOW_CONFIDENCE` — because a template that always sounds
  equally certain regardless of source quality would be actively
  misleading for a technical assistant.
"""

from __future__ import annotations

import hashlib
from typing import List

# ------------------------------------------------------------------
# Phrase bank
# ------------------------------------------------------------------
OPENERS_FACTUAL = [
    "According to {source},",
    "Based on {source},",
    "{source} states that",
    "Per {source},",
    "Looking at {source},",
]

OPENERS_DEFINITION = [
    "Based on {source}, {topic} refers to",
    "According to {source}, {topic} is defined as",
    "{source} describes {topic} as",
]

OPENERS_PROCEDURE = [
    "Here's what {source} says about {topic}:",
    "According to {source}, the steps for {topic} are:",
    "{source} outlines the following for {topic}:",
]

OPENERS_NUMERIC = [
    "According to {source}, the value is",
    "Per {source}, this comes out to",
    "{source} specifies",
]

OPENERS_COMPARISON = [
    "Comparing the sources I found,",
    "Here's how these compare, based on {source}:",
    "Looking across the matching passages,",
]

CLOSERS_OFFER_MORE = [
    "Let me know if you'd like more detail on this.",
    "I can pull up more on this if it would help.",
    "Happy to dig further into this if needed.",
    "",  # sometimes silence is the right close; avoid always tacking on filler
]

HEDGE_LOW_CONFIDENCE = [
    "This was read from a scanned image, so it's worth double-checking against the original: ",
    "This came from OCR on a scan and may not be perfectly accurate — please verify against the source: ",
]

NO_MATCH_RESPONSES = [
    "I couldn't find anything in the indexed documents that answers this. "
    "Try rephrasing, or make sure the relevant folder has been indexed.",
    "Nothing in the current knowledge base matches that. "
    "It might be in a folder that hasn't been indexed yet, or worth trying different wording.",
]

CLARIFICATION_NEEDED = [
    "Could you say a bit more about what you're looking for? For example, which document or system this relates to.",
    "I want to make sure I point you at the right thing — could you give me a bit more context?",
]

FOLLOW_UP_ACK = [
    "Continuing on {topic}:",
    "Still on {topic} —",
    "Following up on {topic}:",
]

MULTI_RESULT_NOTE = [
    "I found {count} other relevant passages as well.",
    "There are {count} more matching passages if this doesn't fully answer it.",
]


def _seeded_choice(options: List[str], seed_key: str) -> str:
    """Deterministic-per-key pseudo-random choice: same seed_key always
    picks the same phrase (keeps a session's voice consistent turn to
    turn) while different questions/sessions get variety.
    """
    if not options:
        return ""
    digest = hashlib.sha256(seed_key.encode("utf-8")).hexdigest()
    index = int(digest, 16) % len(options)
    return options[index]


def opener_for(intent: str, seed_key: str, **kwargs) -> str:
    bank = {
        "definition": OPENERS_DEFINITION,
        "procedure": OPENERS_PROCEDURE,
        "numeric_spec": OPENERS_NUMERIC,
        "calculation": OPENERS_NUMERIC,
        "comparison": OPENERS_COMPARISON,
    }.get(intent, OPENERS_FACTUAL)
    template = _seeded_choice(bank, seed_key)
    try:
        return template.format(**kwargs)
    except KeyError:
        return template


def closer_for(seed_key: str) -> str:
    return _seeded_choice(CLOSERS_OFFER_MORE, seed_key)


def no_match_response(seed_key: str) -> str:
    return _seeded_choice(NO_MATCH_RESPONSES, seed_key)


def clarification_prompt(seed_key: str) -> str:
    return _seeded_choice(CLARIFICATION_NEEDED, seed_key)


def follow_up_acknowledgement(topic: str, seed_key: str) -> str:
    template = _seeded_choice(FOLLOW_UP_ACK, seed_key)
    return template.format(topic=topic)


def multi_result_note(count: int, seed_key: str) -> str:
    template = _seeded_choice(MULTI_RESULT_NOTE, seed_key)
    return template.format(count=count)


def hedge_for_low_confidence(seed_key: str) -> str:
    return _seeded_choice(HEDGE_LOW_CONFIDENCE, seed_key)
