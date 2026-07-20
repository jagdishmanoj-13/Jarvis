"""
core/background_indexer.py
============================

Runs `index_folder()` on a background thread and exposes progress through
a small thread-safe status object, so the UI (or any caller) can poll
progress without blocking.

Design decisions
-----------------
- Plain `threading.Thread`, not `asyncio` or a task queue library: the
  spec explicitly calls for "multithreading", "background indexing",
  "non-blocking UI" using lightweight tooling suitable for a Citrix box,
  and Streamlit's execution model (script reruns on every interaction) is
  most simply integrated with polling a shared, lock-protected status
  object rather than awaiting a coroutine.
- `IndexingJob` holds its own `threading.Lock` around the mutable status
  fields (`done`, `total`, `current_file`, `stats`, `error`) since the
  background thread writes them while the UI thread reads them
  concurrently on every Streamlit rerun.
- `JobRegistry` is a process-wide dict of jobs keyed by folder path, so
  the UI can check "is this folder currently being indexed" and avoid
  starting a second concurrent index of the same folder (which could
  otherwise race on the same SQLite rows / FTS index).
- Exceptions inside the worker thread are caught and stored on the job
  (`error`) rather than being allowed to silently kill the thread — an
  unhandled exception in a background thread does not propagate to the
  main thread or crash the app, so without this the UI would just show a
  job stuck at some percentage forever with no explanation.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Optional

from core.indexing_service import IndexingStats, index_folder
from database.metadata_store import MetadataStore
from utils.logger import get_logger

logger = get_logger(__name__)


@dataclass
class IndexingJob:
    folder: Path
    done: int = 0
    total: int = 0
    current_file: str = ""
    is_running: bool = True
    started_at: float = field(default_factory=time.time)
    finished_at: Optional[float] = None
    stats: Optional[IndexingStats] = None
    error: Optional[str] = None
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def snapshot(self) -> dict:
        with self._lock:
            fraction = (self.done / self.total) if self.total else (0.0 if self.is_running else 1.0)
            return {
                "folder": str(self.folder), "done": self.done, "total": self.total,
                "fraction": fraction, "current_file": self.current_file,
                "is_running": self.is_running, "stats": self.stats, "error": self.error,
                "elapsed_seconds": (self.finished_at or time.time()) - self.started_at,
            }

    def _on_progress(self, done: int, total: int, filename: str) -> None:
        with self._lock:
            self.done, self.total, self.current_file = done, total, filename

    def _run(self, store: MetadataStore) -> None:
        try:
            stats = index_folder(store, self.folder, progress_callback=self._on_progress)
            with self._lock:
                self.stats = stats
        except Exception as exc:  # a background thread must never die silently
            logger.exception("Background indexing job failed for %s", self.folder)
            with self._lock:
                self.error = str(exc)
        finally:
            with self._lock:
                self.is_running = False
                self.finished_at = time.time()


class JobRegistry:
    """Process-wide registry of indexing jobs, keyed by resolved folder path."""

    def __init__(self):
        self._jobs: Dict[str, IndexingJob] = {}
        self._lock = threading.Lock()

    def start(self, store: MetadataStore, folder: Path) -> IndexingJob:
        key = str(folder.resolve())
        with self._lock:
            existing = self._jobs.get(key)
            if existing and existing.is_running:
                return existing  # already indexing this folder; don't start a duplicate

            job = IndexingJob(folder=folder)
            self._jobs[key] = job

        thread = threading.Thread(target=job._run, args=(store,), daemon=True, name=f"jarvis-index-{folder.name}")
        thread.start()
        return job

    def get(self, folder: Path) -> Optional[IndexingJob]:
        return self._jobs.get(str(folder.resolve()))

    def all_jobs(self) -> Dict[str, IndexingJob]:
        return dict(self._jobs)


_registry_singleton: Optional[JobRegistry] = None


def get_job_registry() -> JobRegistry:
    global _registry_singleton
    if _registry_singleton is None:
        _registry_singleton = JobRegistry()
    return _registry_singleton
