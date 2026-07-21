"""
retrieval/hybrid_search.py
=============================

The real Retrieval Engine the spec called for: Hybrid Search combining
keyword, synonym, fuzzy, metadata, and table-aware signals into one
ranked result list — not just a raw FTS5 keyword pass.

Design decisions
-----------------
- Layered, not monolithic: each signal (`_keyword_pass`, `_synonym_pass`,
  `_fuzzy_pass`, `_metadata_pass`) is its own function returning
  `(chunk_row, reason_tag, raw_score)` tuples, merged by `_rank_and_merge`.
  This mirrors the spec's module list (Keyword Search / Fuzzy Match /
  Synonym Search / Metadata Search / Ranking Engine) as clearly separated,
  independently testable pieces rather than one tangled query.
- **Keyword** stays on SQLite FTS5 (already indexed, no extra storage).
- **Synonym** expansion uses the bundled domain lexicon
  (`language/lexicon.py`) — zero network, zero model. A term with a known
  synonym gets a second, lower-weighted FTS pass on the synonym so a
  document that says "fastener" surfaces for a query about "bolt".
- **Fuzzy** matching handles typos ("torqe" -> "torque"). Since FTS5 has no
  built-in fuzzy support, this builds a small in-memory vocabulary of
  distinct significant words actually present in the corpus (cached per
  document-count via `CacheManager`, so it's rebuilt only when the corpus
  changes, not on every search) and uses `difflib.get_close_matches` —
  stdlib only, no extra dependency — to find near-miss terms, then reruns
  a keyword pass on the corrected term.
- **Metadata** matching: a query term that exactly matches a filename
  (stem) gets every chunk of that document boosted — "what's in
  torque_spec" should surface torque_spec.docx even if "torque_spec"
  itself never appears inside the document body.
- **Ranking**: combines FTS5's bm25 rank (lower = better, so it's
  inverted), a flat per-signal bonus (keyword > synonym > fuzzy, reflecting
  descending confidence), a metadata-match bonus, and a table-element
  boost when the query looks like a numeric/spec lookup (tables are what
  actually answer those). This directly addresses the "required PPE
  gloves" gap from earlier testing: PPE and gloves live in different
  chunks, and no single signal here fixes that alone, but keyword recall
  (OR-matching) plus ranking still surfaces both, ordered by relevance
  instead of returning nothing or returning noise.
"""

from __future__ import annotations

import difflib
import json
import re
from dataclasses import dataclass, field
from typing import List, Optional

from cache.cache_manager import get_cache
from database.metadata_store import MetadataStore
from language.lexicon import synonyms_for
from utils.logger import get_logger

logger = get_logger(__name__)

_STOPWORDS = {
    "what", "is", "are", "the", "a", "an", "was", "were", "do", "does", "did",
    "how", "why", "when", "where", "which", "who", "and", "or", "of", "for",
    "to", "in", "on", "at", "about", "this", "that", "it", "with", "i",
}

_SCORE_KEYWORD = 10.0
_SCORE_SYNONYM = 5.0
_SCORE_FUZZY = 3.0
_SCORE_METADATA_MATCH = 8.0
_SCORE_TABLE_BOOST = 4.0
_NUMERIC_QUERY_RE = re.compile(
    r"\b(torque|value|spec|rating|tolerance|dimension|pressure|temperature|weight|size|voltage|current|average|mean|total|sum)\b",
    re.IGNORECASE,
)


@dataclass
class SearchHit:
    document_id: str
    filename: str
    path: str
    chunk_id: str
    text: str
    element_type: str
    page_number: Optional[int]
    section_path: Optional[str]
    score: float
    match_reasons: List[str] = field(default_factory=list)
    extra: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        """Backward-compatible dict view so existing callers (language
        generation, UI) that expect dict-shaped hits keep working
        unchanged.
        """
        return {
            "document_id": self.document_id, "filename": self.filename, "path": self.path,
            "chunk_id": self.chunk_id, "text": self.text, "element_type": self.element_type,
            "page_number": self.page_number, "section_path": self.section_path,
            "score": self.score, "match_reasons": self.match_reasons, "extra": self.extra,
        }


def _content_terms(query: str) -> List[str]:
    words = re.findall(r"[A-Za-z0-9][A-Za-z0-9\-]*", query.lower())
    terms = [w for w in words if w not in _STOPWORDS and len(w) > 1]
    return terms or words  # if the query was ALL stopwords, fall back to everything


def _fts_or_query(terms: List[str]) -> Optional[str]:
    return " OR ".join(terms) if terms else None


def _run_fts(store: MetadataStore, terms: List[str], limit: int) -> List[dict]:
    fts_query = _fts_or_query(terms)
    if not fts_query:
        return []
    try:
        with store._connect() as conn:
            rows = conn.execute(
                """
                SELECT d.filename, d.path, d.document_id, c.chunk_id, c.text, c.element_type,
                       c.page_number, c.section_path, c.extra_json, bm25(chunks_fts) AS rank
                FROM chunks_fts
                JOIN chunks c ON c.rowid = chunks_fts.rowid
                JOIN documents d ON d.document_id = c.document_id
                WHERE chunks_fts MATCH ?
                ORDER BY rank
                LIMIT ?
                """,
                (fts_query, limit),
            ).fetchall()
            return [dict(r) for r in rows]
    except Exception as exc:
        logger.warning("FTS query failed for terms %r: %s", terms, exc)
        return []


def _get_vocabulary(store: MetadataStore) -> List[str]:
    """Distinct significant words present in the corpus, cached until the
    document count changes. Used only as a fuzzy-match candidate pool, not
    for ranking.
    """
    cache = get_cache()
    with store._connect() as conn:
        doc_count = conn.execute("SELECT COUNT(*) AS n FROM documents").fetchone()["n"]
    cache_key = f"vocabulary_{doc_count}"

    cached = cache.get("text", cache_key)
    if cached is not None:
        return cached

    with store._connect() as conn:
        rows = conn.execute("SELECT text FROM chunks LIMIT 5000").fetchall()
    word_counts: dict = {}
    for row in rows:
        for w in re.findall(r"[A-Za-z]{3,}", row["text"].lower()):
            if w in _STOPWORDS:
                continue
            word_counts[w] = word_counts.get(w, 0) + 1
    vocabulary = sorted(word_counts, key=word_counts.get, reverse=True)[:3000]
    cache.set("text", cache_key, vocabulary)
    return vocabulary


def _synonym_pass(store: MetadataStore, terms: List[str], limit: int) -> List[dict]:
    synonym_terms: List[str] = []
    for t in terms:
        for syn in synonyms_for(t):
            synonym_terms.extend(re.findall(r"[A-Za-z0-9\-]+", syn.lower()))
    synonym_terms = [t for t in synonym_terms if t not in _STOPWORDS]
    if not synonym_terms:
        return []
    return _run_fts(store, synonym_terms, limit)


def _fuzzy_pass(store: MetadataStore, terms: List[str], limit: int, already_hit_terms: set) -> List[dict]:
    """Only runs fuzzy correction for terms that didn't already match
    anything via keyword/synonym search — no point fuzzy-matching a term
    that already found real hits.
    """
    unmatched = [t for t in terms if t not in already_hit_terms and len(t) > 3]
    if not unmatched:
        return []
    vocabulary = _get_vocabulary(store)
    if not vocabulary:
        return []

    corrected_terms = []
    for t in unmatched:
        matches = difflib.get_close_matches(t, vocabulary, n=2, cutoff=0.78)
        corrected_terms.extend(m for m in matches if m != t)

    if not corrected_terms:
        return []
    return _run_fts(store, corrected_terms, limit)


def _metadata_pass(store: MetadataStore, terms: List[str], limit: int) -> List[dict]:
    """Boosts an entire document when a query term matches its filename."""
    hits: List[dict] = []
    all_docs = store.list_documents()
    for term in terms:
        for doc in all_docs:
            stem = doc.filename.rsplit(".", 1)[0].lower()
            if term == stem or term in stem.split("_") or term in stem.split("-"):
                for chunk in store.get_chunks_for_document(doc.document_id)[:limit]:
                    hits.append({
                        "filename": doc.filename, "path": doc.path, "document_id": doc.document_id,
                        "chunk_id": chunk.chunk_id, "text": chunk.text,
                        "element_type": chunk.element_type.value, "page_number": chunk.page_number,
                        "section_path": chunk.section_path, "rank": 0.0, "extra": chunk.extra,
                    })
    return hits


def hybrid_search(store: MetadataStore, query: str, limit: int = 8) -> List[SearchHit]:
    query = (query or "").strip()
    if not query:
        return []

    terms = _content_terms(query)
    is_numeric_query = bool(_NUMERIC_QUERY_RE.search(query))

    keyword_rows = _run_fts(store, terms, limit * 3)
    matched_terms = {t for t in terms if any(t in (row["text"] or "").lower() for row in keyword_rows)}

    synonym_rows = _synonym_pass(store, terms, limit * 2)
    fuzzy_rows = _fuzzy_pass(store, terms, limit * 2, matched_terms)
    metadata_rows = _metadata_pass(store, terms, limit)

    merged: dict = {}  # chunk_id -> SearchHit

    def _add(rows, base_score, reason):
        for row in rows:
            chunk_id = row["chunk_id"]
            bm25_rank = row.get("rank", 0.0) or 0.0
            score_contribution = base_score - (bm25_rank * 0.1)  # lower bm25 rank = better match = higher score
            if row.get("element_type") == "table" and is_numeric_query:
                score_contribution += _SCORE_TABLE_BOOST

            extra = row.get("extra")
            if extra is None:
                raw = row.get("extra_json")
                try:
                    extra = json.loads(raw) if raw else {}
                except (json.JSONDecodeError, TypeError):
                    extra = {}

            if chunk_id in merged:
                merged[chunk_id].score += score_contribution
                if reason not in merged[chunk_id].match_reasons:
                    merged[chunk_id].match_reasons.append(reason)
            else:
                merged[chunk_id] = SearchHit(
                    document_id=row["document_id"], filename=row["filename"], path=row["path"],
                    chunk_id=chunk_id, text=row["text"], element_type=row["element_type"],
                    page_number=row["page_number"], section_path=row["section_path"],
                    score=score_contribution, match_reasons=[reason], extra=extra,
                )

    _add(keyword_rows, _SCORE_KEYWORD, "keyword")
    _add(synonym_rows, _SCORE_SYNONYM, "synonym")
    _add(fuzzy_rows, _SCORE_FUZZY, "fuzzy")
    _add(metadata_rows, _SCORE_METADATA_MATCH, "filename_match")

    ranked = sorted(merged.values(), key=lambda h: h.score, reverse=True)
    return ranked[:limit]


def search_chunks_compat(store: MetadataStore, query: str, limit: int = 8) -> List[dict]:
    """Dict-shaped compatibility wrapper for callers written against the
    original Phase-2 `core.indexing_service.search_chunks` interface.
    """
    return [h.as_dict() for h in hybrid_search(store, query, limit)]
