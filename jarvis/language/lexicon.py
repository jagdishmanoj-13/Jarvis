"""
language/lexicon.py
====================

A small, hand-built, fully-bundled linguistic knowledge base: irregular
word forms, inflection rules, and word-class hints. This is the
"grammar database" — it ships as Python data in this file (no network
fetch, no NLTK corpus download), so it works on a fully offline Citrix
machine on day one.

Design decisions
-----------------
- English pluralization/singularization is handled with the standard
  ordered-rule algorithm (check irregulars first, then suffix patterns
  from most-specific to least-specific: -us/-is/-ex/-ch/-sh/-y/-f/-o
  before the general -s fallback). This is the same rule shape used by
  well-known libraries like `inflect`, reimplemented here from scratch to
  avoid adding a dependency for a genuinely small amount of logic.
- Irregular verbs/nouns common in engineering/technical writing
  (datum/data, criterion/criteria, analysis/analyses, index/indices) are
  listed explicitly since the suffix rules would get these wrong.
- This is intentionally NOT a full lexical database like WordNet (no
  internet available in the build/target environment to fetch one). If a
  richer synonym/definition lexicon is wanted later, `nltk.corpus.wordnet`
  can be installed offline (IT downloads the corpus zip once, copies it to
  the machine, points `NLTK_DATA` at it) and layered in without changing
  any other module — `synonyms_for()` below is the seam where that would
  plug in.
"""

from __future__ import annotations

import re

_IRREGULAR_PLURALS = {
    "datum": "data", "criterion": "criteria", "analysis": "analyses",
    "index": "indices", "matrix": "matrices", "vertex": "vertices",
    "phenomenon": "phenomena", "appendix": "appendices", "axis": "axes",
    "child": "children", "person": "people", "man": "men", "woman": "women",
    "foot": "feet", "tooth": "teeth", "mouse": "mice",
}
_IRREGULAR_SINGULARS = {v: k for k, v in _IRREGULAR_PLURALS.items()}

_UNCOUNTABLE = {
    "equipment", "information", "software", "hardware", "steel", "aluminum",
    "torque", "data", "feedback", "maintenance", "safety", "documentation",
}

_SUFFIX_RULES = [
    (re.compile(r"(x|ch|sh|s|z)$", re.IGNORECASE), lambda m, w: w + "es"),
    (re.compile(r"([^aeiou])y$", re.IGNORECASE), lambda m, w: w[:-1] + "ies"),
    (re.compile(r"(fe)$", re.IGNORECASE), lambda m, w: w[:-2] + "ves"),
    (re.compile(r"([^f])f$", re.IGNORECASE), lambda m, w: w[:-1] + "ves"),
    (re.compile(r"o$", re.IGNORECASE), lambda m, w: w + "es"),
]


def pluralize(word: str) -> str:
    if not word:
        return word
    lower = word.lower()
    if lower in _UNCOUNTABLE:
        return word
    if lower in _IRREGULAR_PLURALS:
        return _IRREGULAR_PLURALS[lower]
    for pattern, transform in _SUFFIX_RULES:
        if pattern.search(word):
            return transform(pattern.search(word), word)
    return word + "s"


def singularize(word: str) -> str:
    if not word:
        return word
    lower = word.lower()
    if lower in _IRREGULAR_SINGULARS:
        return _IRREGULAR_SINGULARS[lower]
    if lower.endswith("ies") and len(word) > 3:
        return word[:-3] + "y"
    if lower.endswith("ves"):
        return word[:-3] + "f"
    if lower.endswith("es") and lower[:-2].endswith(("x", "ch", "sh", "s", "z")):
        return word[:-2]
    if lower.endswith("s") and not lower.endswith("ss"):
        return word[:-1]
    return word


def pluralize_if(word: str, count: float) -> str:
    """Returns the correctly-inflected form of `word` for `count` items."""
    return word if count == 1 else pluralize(word)


_VOWEL_SOUND_EXCEPTIONS = {"hour", "honest", "honor", "heir"}  # silent-h words that take "an"
_CONSONANT_SOUND_EXCEPTIONS = {"university", "user", "european", "one", "unit"}  # 'y'/'w' sound words that take "a"


def choose_article(word: str) -> str:
    """Returns 'a' or 'an' for the given word, handling common phonetic
    exceptions (an hour, a university) that simple vowel-letter checks get
    wrong.
    """
    if not word:
        return "a"
    lower = word.lower().strip()
    first_word = lower.split()[0] if lower.split() else lower
    if first_word in _VOWEL_SOUND_EXCEPTIONS:
        return "an"
    if first_word in _CONSONANT_SOUND_EXCEPTIONS:
        return "a"
    return "an" if first_word[0] in "aeiou" else "a"


_BE_VERB_FORMS = {"singular_present": "is", "plural_present": "are",
                   "singular_past": "was", "plural_past": "were"}


def agree_be_verb(is_plural: bool, past_tense: bool = False) -> str:
    key = ("plural" if is_plural else "singular") + "_" + ("past" if past_tense else "present")
    return _BE_VERB_FORMS[key]


def agree_have_verb(is_plural_or_you: bool) -> str:
    return "have" if is_plural_or_you else "has"


# Seam for a future richer lexicon (e.g. offline-installed WordNet). Kept
# deliberately tiny and domain-relevant rather than empty/fake.
_DOMAIN_SYNONYMS = {
    "torque": ["rotational force", "turning force"],
    "spec": ["specification", "requirement"],
    "bolt": ["fastener", "screw"],
    "inspect": ["examine", "check", "review"],
    "fail": ["malfunction", "break down"],
    "manual": ["guide", "handbook"],
}


def synonyms_for(word: str) -> list:
    return _DOMAIN_SYNONYMS.get(word.lower(), [])
