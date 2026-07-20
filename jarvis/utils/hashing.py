"""
utils/hashing.py
=================

File hashing and change-detection helpers.

Design decisions
-----------------
- Hashing is streamed in fixed-size chunks (`Settings.hash_chunk_bytes`)
  rather than reading whole files into memory, since engineering document
  stores routinely contain large PDFs/CAD exports and we must stay
  memory-safe on constrained Citrix VMs.
- We hash file *content*, not just mtime/size, because network drives and
  Citrix profile redirection can produce unreliable mtimes (e.g. a file
  copied between shares can get a new mtime without any content change).
  Content hash is the source of truth for "did this file actually change".
- A cheap `quick_fingerprint` (size + mtime) is provided as a fast
  pre-filter: the indexer checks the cheap fingerprint first and only pays
  for a full content hash when the fingerprint looks like it might have
  changed. This keeps re-scans of large, mostly-unchanged folders fast.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

from config.settings import get_settings


@dataclass(frozen=True)
class QuickFingerprint:
    size_bytes: int
    mtime_ns: int

    def as_string(self) -> str:
        return f"{self.size_bytes}:{self.mtime_ns}"


def quick_fingerprint(path: Path) -> QuickFingerprint:
    """Cheap, no-content-read fingerprint used as a fast change pre-filter."""
    stat = path.stat()
    return QuickFingerprint(size_bytes=stat.st_size, mtime_ns=stat.st_mtime_ns)


def compute_file_hash(path: Path, algorithm: str | None = None) -> str:
    """Compute a streaming content hash of a file.

    Raises FileNotFoundError / PermissionError naturally; callers in the
    indexing pipeline are expected to catch and log these (a file can be
    deleted or locked between directory scan and hash time).
    """
    settings = get_settings()
    algo_name = algorithm or settings.hash_algorithm
    hasher = hashlib.new(algo_name)

    with open(path, "rb") as f:
        while True:
            block = f.read(settings.hash_chunk_bytes)
            if not block:
                break
            hasher.update(block)

    return hasher.hexdigest()


def compute_text_hash(text: str, algorithm: str = "sha256") -> str:
    """Hash extracted text (used to detect duplicate chunks / cache keys)."""
    hasher = hashlib.new(algorithm)
    hasher.update(text.encode("utf-8", errors="ignore"))
    return hasher.hexdigest()
