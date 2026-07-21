# JARVIS — Engineering Knowledge Assistant

An enterprise, fully-local AI assistant for engineering/company knowledge.
Built module-by-module, in dependency order, so every layer is independently
testable and the future custom transformer model can be dropped in without
touching anything else.

## Hard constraints this design honors
- **No pretrained LLM** anywhere in the pipeline. `language/` is an
  interface only (built in a later phase) — retrieval, reasoning, and
  language generation are strictly separated.
- **Citrix-safe**: no Docker, no GPU, CPU-only, offline-capable, minimal
  and mostly-pure-Python dependencies, writes only to the user's local
  profile (`%LOCALAPPDATA%\JARVIS` by default).
- **Local-only data**: SQLite + local disk cache, no cloud calls.

## Build status

| Phase | Layer | Status |
|---|---|---|
| 1 | `config/`, `utils/`, `models/`, `database/`, `cache/` — foundations | ✅ Done |
| 2 | `parser/` — file parsing for every supported format + chunking | ✅ Done |
| 3 | `ocr/`, `core/archive_service.py`, `core/background_indexer.py` — OCR, ZIP expansion, non-blocking threaded indexing | ✅ Done |
| 4 | `retrieval/hybrid_search.py` — real hybrid keyword + fuzzy + synonym + metadata search with ranking | ✅ Done |
| 5 | `reasoning/intent_detector.py`, `memory/context_manager.py` — rule-based intent + follow-up context | ✅ Done |
| 6 | `language/` — non-LLM LanguageGenerationEngine (grammar + communication + math) | ✅ Done |
| 7 | `ui/` — full premium glassmorphic UI (theme, particles, progress rings, document map, favorites) | ✅ Done |

**All 7 phases are implemented and integration-tested together** — see
"Full-system regression" below. Every module was tested individually as it
was built, then re-verified as part of the complete pipeline before being
called done.

## What Phase 4 added: real hybrid search, not just keyword matching
`retrieval/hybrid_search.py` layers four independent signals, each
separately testable, then merges and ranks them:
- **Keyword** — SQLite FTS5, stopword-filtered, OR-matched for recall.
- **Synonym** — expands query terms via the bundled domain lexicon
  (`language/lexicon.py`: "bolt" ↔ "fastener", etc.) with a second, lower-
  weighted search pass.
- **Fuzzy** — typo-tolerant matching via `difflib` (stdlib) against a
  cached corpus vocabulary, so "torqe" still finds "torque" content.
- **Metadata** — a query term matching a filename boosts that whole
  document's chunks (asking about "torque_spec" surfaces torque_spec.docx
  even if that exact phrase never appears inside the file).

All four are combined by a ranking function that also boosts table-element
chunks for numeric/spec-sounding queries, since tables are what actually
answer those. Verified with real typo and synonym queries, not just
described — see the development history for test transcripts.

## What Phase 7 added: the actual premium UI
`ui/theme.py` (glassmorphic dark/light CSS + animated particle background,
injected via one stylesheet driven by CSS variables) and
`ui/visualizations.py` (a real, data-driven progress ring and document
knowledge map, rendered as generated SVG — deliberately built on pure
Python + `math`, not `matplotlib`/`networkx`/`plotly`, to keep the
dependency footprint Citrix-light) sit on top of the unchanged Phase 1-6
backend. Also added: a dark/light theme toggle, a system resource monitor
(CPU/RAM via `psutil`, gracefully hidden if unavailable), word-by-word
"typing" reveal of composed answers, and a favorites/bookmarks panel — all
persisted via `MetadataStore` so they survive a restart.

## Full-system regression
Every phase was re-verified together, not just individually, before this
was called complete: full folder index (all parsers + OCR + ZIP +
background threading) → incremental re-index (confirms nothing
unnecessarily re-parsed) → 9 real questions spanning every intent type
(definition, calculation, unit conversion, table statistics, follow-up,
list, comparison, no-match, and a deliberate typo) → SVG visualization
validity against live data → full UI module import with every real
dependency wired in. All passed.

## Running it (Windows)
See **`HOW_TO_RUN.txt`** at the top of this package for copy-paste
commands. (There's no double-click `.bat` launcher bundled here on
purpose — email attachment scanners, including Gmail's, block or strip
zip files containing script files like `.bat`, even renamed ones, because
they inspect file content rather than just the extension. `HOW_TO_RUN.txt`
also shows how to create your own local double-click shortcut in 30
seconds once you have it running, since a file you type yourself never
touches an email scanner.)

**What actually works right now:** point it at a folder, it indexes every
supported file (including OCR for scanned pages/images and ZIP archives),
and you can ask keyword-style questions and get the best-matching passage
back with a file+page/section citation. There is no language-generation
model summarizing/rephrasing answers yet — that's Phase 6, by design (see
the top of this file). Search is FTS5 keyword matching, not the full
hybrid/fuzzy/synonym engine — that's Phase 4.

## Running it (any OS, dev/testing)

## Layout
```
jarvis/
├── config/       # Settings singleton, all paths & feature flags
├── utils/        # logging, file hashing / change detection
├── models/       # dataclasses shared by every layer (Document, Chunk, ...)
├── database/     # SQLite MetadataStore (documents, chunks, folders, memory)
├── cache/        # content-hash-keyed disk cache (text/OCR/tables/summaries)
├── parser/       # BaseParser interface + one class per file format + chunker
├── ocr/          # (Phase 3)
├── retrieval/    # (Phase 4)
├── reasoning/    # (Phase 5)
├── memory/       # (Phase 5)
├── language/     # (Phase 6)
├── ui/           # (Phase 7, Streamlit)
├── styles/       # (Phase 7)
├── assets/       # (Phase 7)
└── requirements.txt
```

## How the parser layer works (Phase 2)
1. `parser/registry.py` maps a file extension → a `BaseParser` instance.
   Formats needing an optional dependency register an `UnavailableParser`
   with a clear reason if that dependency is missing, instead of crashing.
2. Each concrete parser (`pdf_parser.py`, `docx_parser.py`, `pptx_parser.py`,
   `tabular_parser.py`, `text_family_parser.py`) turns one file into a
   `ParsedDocument`: coarse metadata + an ordered list of `RawElement`s
   (text tagged as BODY_TEXT / HEADING / TABLE / CAPTION / HYPERLINK / ...).
3. `parser/chunker.py` turns those raw elements into retrieval-sized
   `Chunk` objects: body text is merged with sliding-window overlap up to
   `Settings.chunk_size_tokens`; tables/captions/hyperlinks are never merged
   so citations can point at them precisely.
4. Chunks are stored via `MetadataStore.replace_chunks_for_document()`,
   which also updates the FTS5 keyword-search index automatically via
   triggers.

Currently working parsers (verified against real sample files, not mocks):
PDF, DOCX, PPTX, XLSX, CSV, TXT, LOG, MD, JSON, YAML, INI, HTML, XML, RTF,
and source code (`.py .java .cs .cpp .c .h .sql`).

Explicitly deferred (registered as `UnavailableParser` with a clear reason,
not silently mishandled): legacy `.doc`/`.ppt` (need an external converter),
images & scanned-PDF pages (→ Phase 3 OCR), `.zip` (→ Phase 3 archive
expansion), `.eml`/`.msg` (→ later phase).

## Running the smoke tests
```bash
cd jarvis
export JARVIS_HOME=/tmp/jarvis_test_home   # optional; isolates test data
pip install -r requirements.txt
python3 -c "
from parser.registry import get_parser_for
from parser.chunker import TextChunker
from pathlib import Path
p = get_parser_for(Path('some_file.docx'))
parsed = p.parse(Path('some_file.docx'))
chunks = TextChunker().chunk('doc-id-1', parsed)
print(len(chunks), 'chunks produced')
"
```
