"""
config/settings.py
===================

Central configuration for JARVIS.

Design decisions
-----------------
1. A single dataclass-based settings object (`Settings`) is the one source of
   truth. Every other module receives configuration by importing
   `get_settings()` rather than reading environment variables or files
   directly. This makes the whole system testable (tests can construct a
   `Settings` instance pointing at a temp directory) and keeps Citrix/offline
   constraints in one place instead of scattered across modules.

2. All paths are resolved relative to a single `JARVIS_HOME` root
   (defaults to `%LOCALAPPDATA%/JARVIS` on Windows, `~/.jarvis` elsewhere).
   This matters on Citrix: users often have redirected/roaming profiles with
   restricted write access to `Program Files` or the install directory, but
   `%LOCALAPPDATA%` is reliably writable.

3. No dependency on python-dotenv or similar to keep the dependency surface
   minimal (Citrix = limited installation permissions). A plain `.ini`/`.json`
   override file is supported instead, read with the standard library only.

4. Feature flags (e.g. ENABLE_OCR, ENABLE_SEMANTIC_SEARCH) let the app
   degrade gracefully in environments where optional heavy dependencies
   (e.g. an OCR engine or a vector library) are not installable.
"""

from __future__ import annotations

import json
import os
import platform
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import List


def _default_home() -> Path:
    """Pick a writable, per-user root directory.

    Citrix / locked-down Windows environments frequently deny write access
    to the application's install directory but always allow writes to the
    user's local profile. We therefore default to LOCALAPPDATA on Windows
    and the user's home directory elsewhere, and allow an explicit override
    via the JARVIS_HOME environment variable for IT-managed deployments.
    """
    override = os.environ.get("JARVIS_HOME")
    if override:
        return Path(override).expanduser().resolve()

    if platform.system() == "Windows":
        local_app_data = os.environ.get("LOCALAPPDATA")
        if local_app_data:
            return Path(local_app_data) / "JARVIS"
        return Path.home() / "AppData" / "Local" / "JARVIS"

    return Path.home() / ".jarvis"


@dataclass
class Settings:
    # --- Root paths -------------------------------------------------
    home_dir: Path = field(default_factory=_default_home)

    # --- Derived paths (populated in __post_init__) ------------------
    db_path: Path = field(init=False)
    cache_dir: Path = field(init=False)
    vector_store_dir: Path = field(init=False)
    log_dir: Path = field(init=False)
    config_override_path: Path = field(init=False)

    # --- Indexing behaviour -------------------------------------------
    supported_extensions: List[str] = field(default_factory=lambda: [
        ".pdf", ".docx", ".doc", ".ppt", ".pptx", ".xls", ".xlsx", ".csv",
        ".txt", ".rtf", ".html", ".htm", ".xml", ".json", ".yaml", ".yml",
        ".ini", ".log", ".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp",
        ".eml", ".msg", ".zip", ".py", ".java", ".cs", ".cpp", ".c", ".h",
        ".sql", ".md",
    ])
    max_file_size_mb: int = 200
    indexing_thread_pool_size: int = 4
    hash_algorithm: str = "sha256"
    hash_chunk_bytes: int = 1024 * 1024  # 1 MB streaming reads for large files

    # --- Feature flags (allow graceful degradation on locked-down hosts) --
    enable_ocr: bool = True
    enable_semantic_search: bool = True
    enable_gpu: bool = False  # Citrix constraint: always assume CPU-only

    # --- Retrieval / reasoning tuning ---------------------------------
    max_search_results: int = 25
    max_context_chunks: int = 8
    chunk_size_tokens: int = 400
    chunk_overlap_tokens: int = 60

    # --- Conversation memory -------------------------------------------
    max_conversation_turns_in_memory: int = 40

    # --- App identity ---------------------------------------------------
    app_name: str = "JARVIS"
    app_tagline: str = "Engineering Knowledge Assistant"
    app_version: str = "0.1.0-phase1"

    def __post_init__(self) -> None:
        self.home_dir = Path(self.home_dir)
        self.db_path = self.home_dir / "database" / "jarvis_metadata.sqlite3"
        self.cache_dir = self.home_dir / "cache"
        self.vector_store_dir = self.home_dir / "vector_store"
        self.log_dir = self.home_dir / "logs"
        self.config_override_path = self.home_dir / "config_override.json"

        self._ensure_directories()
        self._apply_overrides()

    def _ensure_directories(self) -> None:
        for path in (
            self.home_dir,
            self.db_path.parent,
            self.cache_dir,
            self.vector_store_dir,
            self.log_dir,
        ):
            path.mkdir(parents=True, exist_ok=True)

    def _apply_overrides(self) -> None:
        """Merge a user/IT-provided JSON override file, if present.

        This lets an enterprise deployment tweak settings (e.g. disable OCR
        on a machine with no OCR binary available) without touching code.
        """
        if not self.config_override_path.exists():
            return
        try:
            overrides = json.loads(self.config_override_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return
        for key, value in overrides.items():
            if hasattr(self, key):
                setattr(self, key, value)

    def as_dict(self) -> dict:
        d = asdict(self)
        # Path objects aren't JSON-serialisable by default; stringify them.
        for k, v in d.items():
            if isinstance(v, Path):
                d[k] = str(v)
        return d


_settings_singleton: Settings | None = None


def get_settings() -> Settings:
    """Return the process-wide Settings singleton, creating it on first use."""
    global _settings_singleton
    if _settings_singleton is None:
        _settings_singleton = Settings()
    return _settings_singleton


def reset_settings_for_testing(**overrides) -> Settings:
    """Force-create a fresh Settings instance (used by tests / tools)."""
    global _settings_singleton
    _settings_singleton = Settings(**overrides)
    return _settings_singleton
