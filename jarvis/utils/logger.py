"""
utils/logger.py
================

Centralised logging setup.

Design decisions
-----------------
- Uses only the standard library `logging` module (no `loguru`/`structlog`)
  to respect the Citrix constraint of minimal dependencies.
- Logs to a rotating file under `Settings.log_dir` AND to stdout, so both
  a developer running locally and an IT admin inspecting log files on a
  Citrix session can see what happened.
- `get_logger(name)` mirrors the standard `logging.getLogger(name)` idiom
  so every module just does `logger = get_logger(__name__)`.
- Rotation is size-based (not time-based) because Citrix sessions may not
  stay open long enough for daily rotation to matter, and we want to bound
  disk usage on machines with restricted quotas.
"""

from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

_CONFIGURED = False


def _configure_root_logging(log_dir: Path, level: int = logging.INFO) -> None:
    global _CONFIGURED
    if _CONFIGURED:
        return

    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "jarvis.log"

    formatter = logging.Formatter(
        fmt="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    file_handler = RotatingFileHandler(
        log_file, maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    file_handler.setFormatter(formatter)

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)

    root = logging.getLogger("jarvis")
    root.setLevel(level)
    root.addHandler(file_handler)
    root.addHandler(console_handler)
    root.propagate = False

    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    """Return a namespaced logger under the shared 'jarvis' root logger.

    Configuration is lazy: the first call configures handlers using the
    current Settings; subsequent calls just return a child logger.
    """
    if not _CONFIGURED:
        # Local import avoids a circular import between config <-> utils
        from config.settings import get_settings
        _configure_root_logging(get_settings().log_dir)

    if not name.startswith("jarvis"):
        name = f"jarvis.{name}"
    return logging.getLogger(name)
