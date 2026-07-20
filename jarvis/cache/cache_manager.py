"""
cache/cache_manager.py
=======================

Generic, pluggable disk cache used by every expensive pipeline stage:
text extraction, OCR, table parsing, embeddings, and summarisation.

Design decisions
-----------------
- Keys are content hashes (see `utils.hashing.compute_file_hash`), not file
  paths. This is deliberate: if a file is moved/renamed but its content is
  identical, the cache still hits, and parsing/OCR never runs twice for the
  same bytes. It also means two identical manuals dropped in different
  folders share one cache entry.
- Cache entries are namespaced (`namespace/hash.json` or `.bin`), one
  sub-directory per stage (`text/`, `ocr/`, `tables/`, `embeddings/`,
  `summaries/`, `conversation/`). This keeps the cache human-inspectable
  during debugging on a Citrix box where attaching a debugger is often not
  possible, and lets each namespace be cleared independently (e.g. wiping
  only the OCR cache after swapping OCR engines).
- Values are stored as JSON by default for inspectability; a raw-bytes mode
  is available for binary payloads (e.g. serialized embeddings) via
  `get_bytes`/`set_bytes`.
- No external cache library (diskcache, joblib, etc.) is used, to respect
  the "avoid unnecessary dependencies" / "prefer lightweight libraries"
  Citrix constraint — this is implementable entirely on `pathlib` + `json`.
- Thread-safety: writes go through a per-namespace `threading.Lock` because
  background indexing threads may write cache entries concurrently. Reads
  are lock-free (filesystem reads of a fully-written JSON file are safe).
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any, Optional

from config.settings import get_settings
from utils.logger import get_logger

logger = get_logger(__name__)

_NAMESPACES = ("text", "ocr", "tables", "embeddings", "summaries", "conversation")


class CacheManager:
    """Content-hash-keyed disk cache, one instance shared across the app."""

    def __init__(self, cache_dir: Optional[Path] = None):
        self.cache_dir = cache_dir or get_settings().cache_dir
        self._locks: dict[str, threading.Lock] = {ns: threading.Lock() for ns in _NAMESPACES}
        for ns in _NAMESPACES:
            (self.cache_dir / ns).mkdir(parents=True, exist_ok=True)

    def _path_for(self, namespace: str, key: str, suffix: str = "json") -> Path:
        if namespace not in _NAMESPACES:
            raise ValueError(f"Unknown cache namespace '{namespace}'. Valid: {_NAMESPACES}")
        # Two-level sharding (first 2 hex chars) avoids tens of thousands of
        # files in a single directory, which is slow on network-redirected
        # Citrix profile folders.
        shard = key[:2] if len(key) >= 2 else "misc"
        directory = self.cache_dir / namespace / shard
        directory.mkdir(parents=True, exist_ok=True)
        return directory / f"{key}.{suffix}"

    # ------------------------------------------------------------------
    # JSON-friendly interface (text, tables, summaries, parsed structures)
    # ------------------------------------------------------------------
    def get(self, namespace: str, key: str) -> Optional[Any]:
        path = self._path_for(namespace, key)
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Corrupt cache entry %s: %s (ignoring)", path, exc)
            return None

    def set(self, namespace: str, key: str, value: Any) -> None:
        path = self._path_for(namespace, key)
        with self._locks[namespace]:
            tmp_path = path.with_suffix(".tmp")
            tmp_path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
            tmp_path.replace(path)  # atomic on the same filesystem

    def has(self, namespace: str, key: str) -> bool:
        return self._path_for(namespace, key).exists()

    def invalidate(self, namespace: str, key: str) -> None:
        path = self._path_for(namespace, key)
        if path.exists():
            path.unlink(missing_ok=True)

    # ------------------------------------------------------------------
    # Raw-bytes interface (for future binary payloads, e.g. serialized
    # embedding vectors from the pluggable vector store)
    # ------------------------------------------------------------------
    def get_bytes(self, namespace: str, key: str) -> Optional[bytes]:
        path = self._path_for(namespace, key, suffix="bin")
        return path.read_bytes() if path.exists() else None

    def set_bytes(self, namespace: str, key: str, value: bytes) -> None:
        path = self._path_for(namespace, key, suffix="bin")
        with self._locks[namespace]:
            tmp_path = path.with_suffix(".tmp")
            tmp_path.write_bytes(value)
            tmp_path.replace(path)

    def clear_namespace(self, namespace: str) -> None:
        import shutil
        ns_dir = self.cache_dir / namespace
        if ns_dir.exists():
            shutil.rmtree(ns_dir)
        ns_dir.mkdir(parents=True, exist_ok=True)
        logger.info("Cleared cache namespace '%s'", namespace)

    def stats(self) -> dict:
        """Rough size/count stats per namespace, shown in the UI's system monitor panel."""
        result = {}
        for ns in _NAMESPACES:
            ns_dir = self.cache_dir / ns
            files = list(ns_dir.rglob("*.json")) + list(ns_dir.rglob("*.bin"))
            total_bytes = sum(f.stat().st_size for f in files)
            result[ns] = {"entry_count": len(files), "size_mb": round(total_bytes / (1024 * 1024), 2)}
        return result


_cache_singleton: Optional[CacheManager] = None


def get_cache() -> CacheManager:
    global _cache_singleton
    if _cache_singleton is None:
        _cache_singleton = CacheManager()
    return _cache_singleton
