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
| 2.5 | `core/indexing_service.py` + `ui/app.py` — minimal working UI (MVP, ahead of schedule) | ✅ Done |
| 3 | `ocr/`, `core/archive_service.py`, `core/background_indexer.py` — OCR, ZIP expansion, non-blocking threaded indexing | ✅ Done |
| 4 | `retrieval/` — real hybrid keyword/fuzzy/metadata/table search + ranking | ⏳ Next |
| 5 | `reasoning/`, `memory/` — intent detection, context, conversation memory | ⏳ Planned |
| 6 | `language/` — pluggable LanguageGenerationEngine interface | ⏳ Planned |
| 7 | `ui/` — full premium glassmorphic redesign on top of the same backend calls | ⏳ Planned |

## What Phase 3 added
- **OCR** (`ocr/ocr_engine.py`): pluggable interface, Tesseract-backed by
  default. Standalone images (`.png/.jpg/.tif/.bmp`) are OCR'd via
  `parser/image_parser.py`. Scanned PDF pages are detected automatically
  (`parser/pdf_parser.py` flags any page with suspiciously little
  extractable text) and OCR'd page-by-page, so a PDF with a mix of real
  text pages and scanned pages gets the right treatment for each page.
  If `tesseract`/`poppler` aren't installed on a given machine, OCR-
  dependent formats degrade to a clear "unavailable" message instead of
  breaking the rest of indexing.
- **ZIP archives** (`core/archive_service.py`): expanded into a content-
  hash-keyed cache directory (so re-scanning an unchanged zip costs
  nothing) and their contents indexed under an isolated virtual-folder
  key, so archive contents can never be confused with, or accidentally
  cause deletion of, real sibling files. Includes zip-slip path-traversal
  protection and a zip-bomb size cap — both verified against real
  malicious-archive test cases, not just described.
- **Non-blocking background indexing** (`core/background_indexer.py`):
  `index_folder()` now runs on a daemon thread with thread-safe progress
  you can poll; the Streamlit UI polls it every second so a large folder
  scan no longer freezes the browser tab.

## Running it (Windows)
Double-click **`Start_JARVIS.bat`**. First run creates a private `.venv`
folder (no admin rights needed) and installs `requirements.txt`; every run
after that is fast. It launches the Streamlit UI at `http://localhost:8501`
in your default browser. See the OFFLINE INSTALL note inside the `.bat`
file if this machine has no internet access for the one-time package
install.

**What actually works right now:** point it at a folder, it indexes every
supported file, and you can ask keyword-style questions and get the best-
matching passage back with a file+page/section citation. There is no
language-generation model summarizing/rephrasing answers yet — that's
Phase 6, by design (see the top of this file). Search is FTS5 keyword
matching, not the full hybrid/fuzzy/synonym engine — that's Phase 4.

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
