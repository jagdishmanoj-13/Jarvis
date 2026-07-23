"""
=============================================================================
 ENTERPRISE PORTAL DOCUMENT HUNTER
=============================================================================
A single-file (app.py) Streamlit application that automates searching and
downloading documents from an internal enterprise portal by driving a real,
visible Microsoft Edge browser through Selenium - exactly the way a human
would click, type, and read the page. Restyled with a polished five-step
"Setup -> Login -> Search -> Select -> Download" dashboard.

-----------------------------------------------------------------------------
 WHAT THIS TOOL DELIBERATELY DOES *NOT* DO (and why)
-----------------------------------------------------------------------------
An earlier draft of this tool tried to talk directly to a specific real
company's private, undocumented REST API, captured live session
cookies/CSRF tokens out of an authenticated browser to replay against that
API from a separate script, and deliberately downgraded TLS
(`SECLEVEL=1`, disabled certificate verification) to get past that
server's security posture. That version could not be built or extended:
hijacking session tokens for out-of-band API calls and weakening TLS to
reach a named company's private endpoints is a security/authorization
problem regardless of the stated internal-use intent.

This version instead:
    * Never inspects, guesses, or calls any portal's private API.
    * Never reads or replays session cookies/CSRF tokens outside the
      browser itself - the browser IS the client for every request.
    * Never touches TLS/certificate settings.
    * Never hardcodes a specific company's URL or branding - the portal
      URL and on-screen labels are entered by whoever runs the tool.
    * Discovers search boxes, result rows, and download controls purely
      by DOM heuristics (ARIA/role/label/placeholder/text/data-testid),
      the same way the original brief described, with no portal-specific
      selectors baked in.

-----------------------------------------------------------------------------
 LIBRARY-SET NOTE
-----------------------------------------------------------------------------
Built only against packages confirmed available in the target environment:
    selenium, selenium-manager, streamlit, pandas, openpyxl, beautifulsoup4,
    lxml, tenacity, loguru, pillow, tesseract (CLI), watchdog, pyautogui

Selenium is therefore the sole browser engine (resilience via retries, not
a second engine). OCR shells out to the `tesseract` CLI binary directly
since no Python OCR wrapper is in the approved list.

-----------------------------------------------------------------------------
 ARCHITECTURE
-----------------------------------------------------------------------------
Streamlit is the control dashboard. Selenium drives one persistent,
visible Edge window for the whole session - the same window the user logs
into manually (login is NEVER automated) and the same window all
searching/downloading happens in afterwards. Because the same live
browser object is kept in `st.session_state` across Streamlit reruns
(a normal, single-process, in-memory Python object - not serialized),
there is no need to "reattach" or "recapture" anything between steps.

Run with:   streamlit run app.py
=============================================================================
"""

from __future__ import annotations

import csv
import io
import queue
import re
import shutil
import subprocess
import tempfile
import threading
import time
import traceback
import unicodedata
import zipfile
from abc import ABC, abstractmethod
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Optional

import pandas as pd
import streamlit as st

# =============================================================================
# SECTION 0: OPTIONAL / SOFT DEPENDENCIES
# =============================================================================
try:
    from loguru import logger as _loguru_logger
    LOGURU_AVAILABLE = True
except ImportError:  # pragma: no cover
    LOGURU_AVAILABLE = False
    _loguru_logger = None

try:
    from selenium import webdriver
    from selenium.webdriver.edge.options import Options as EdgeOptions
    from selenium.webdriver.common.by import By
    from selenium.webdriver.common.keys import Keys
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.common.exceptions import TimeoutException as SeleniumTimeoutException
    SELENIUM_AVAILABLE = True
except ImportError:  # pragma: no cover
    SELENIUM_AVAILABLE = False

try:
    from bs4 import BeautifulSoup
    BS4_AVAILABLE = True
except ImportError:  # pragma: no cover
    BS4_AVAILABLE = False

try:
    import lxml  # noqa: F401
    LXML_AVAILABLE = True
except ImportError:  # pragma: no cover
    LXML_AVAILABLE = False

TESSERACT_AVAILABLE = shutil.which("tesseract") is not None

try:
    from watchdog.observers import Observer
    from watchdog.events import FileSystemEventHandler
    WATCHDOG_AVAILABLE = True
except ImportError:  # pragma: no cover
    WATCHDOG_AVAILABLE = False
    class FileSystemEventHandler:  # type: ignore
        pass

try:
    import pyautogui
    PYAUTOGUI_AVAILABLE = True
except ImportError:  # pragma: no cover
    PYAUTOGUI_AVAILABLE = False

try:
    from tenacity import retry as tenacity_retry, stop_after_attempt, wait_exponential  # noqa: F401
    TENACITY_AVAILABLE = True
except ImportError:  # pragma: no cover
    TENACITY_AVAILABLE = False


def _bs4_parser() -> str:
    return "lxml" if LXML_AVAILABLE else "html.parser"


# =============================================================================
# SECTION 1: PAGE CONFIG + BRAND THEME (generic, user-editable - never hardcoded)
# =============================================================================
st.set_page_config(
    page_title="Enterprise Portal Document Hunter",
    page_icon="🗂️",
    layout="wide",
    initial_sidebar_state="collapsed",
)

st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700;800&family=JetBrains+Mono:wght@400;500&display=swap');
html,body,[class*="css"]{font-family:'Inter',sans-serif;}
.stApp{background:#f0f4fa;}

.hdr{background:linear-gradient(135deg,#0f3b8c,#1a4fad 60%,#1d6fdb);
  border-radius:12px;padding:18px 26px;margin-bottom:22px;
  display:flex;align-items:center;gap:16px;
  box-shadow:0 4px 20px rgba(26,79,173,.22);position:relative;overflow:hidden;}
.hdr::after{content:'';position:absolute;right:0;top:0;bottom:0;width:7px;
  background:linear-gradient(180deg,#f5a623,#e8890a);}
.logo-chip{background:#fff;color:#1a4fad;font-weight:800;font-size:.95rem;
  padding:6px 12px;border-radius:5px;letter-spacing:1px;flex-shrink:0;}
.hdr-txt h1{font-size:1.25rem;font-weight:800;color:#fff;margin:0 0 2px 0;}
.hdr-txt p{font-size:.8rem;color:#c0d4f5;margin:0;}

.steps{display:flex;gap:0;margin-bottom:22px;}
.step{flex:1;padding:10px 0;text-align:center;font-size:.73rem;font-weight:600;
  color:#94a3b8;background:#fff;border:1px solid #dce6f5;border-right:none;}
.step:first-child{border-radius:8px 0 0 8px;}
.step:last-child{border-radius:0 8px 8px 0;border-right:1px solid #dce6f5;}
.step.active{background:#1a4fad;color:#fff;border-color:#1a4fad;}
.step.done{background:#d1fae5;color:#065f46;border-color:#a7f3d0;}
.step .snum{display:block;font-size:1.1rem;font-weight:800;}

.card{background:#fff;border:1px solid #dce6f5;border-radius:10px;
  padding:20px 24px;margin-bottom:14px;}
.card-title{font-size:.72rem;font-weight:700;text-transform:uppercase;
  letter-spacing:1.1px;color:#1a4fad;margin-bottom:14px;
  display:flex;align-items:center;gap:8px;}
.dot{width:8px;height:8px;border-radius:50%;
  background:linear-gradient(135deg,#f5a623,#e8890a);flex-shrink:0;}

.result-hdr{display:grid;grid-template-columns:50px 1fr 160px 100px;
  gap:8px;padding:8px 14px;background:#f1f5fb;border-radius:7px 7px 0 0;
  border:1px solid #dce6f5;font-size:.72rem;font-weight:700;
  color:#475569;text-transform:uppercase;letter-spacing:.8px;}
.result-row{display:grid;grid-template-columns:50px 1fr 160px 100px;
  gap:8px;padding:10px 14px;border:1px solid #dce6f5;border-top:none;
  align-items:center;background:#fff;}
.result-row:last-child{border-radius:0 0 7px 7px;}
.doc-title{color:#1e293b;font-size:.84rem;font-weight:500;line-height:1.3;}
.doc-id{color:#64748b;font-size:.75rem;margin-top:2px;}
.lib-badge{background:#e0e9fa;color:#1a4fad;border-radius:12px;
  padding:2px 9px;font-size:.72rem;font-weight:600;white-space:nowrap;}

.logbox{background:#0f172a;border:1px solid #1e293b;border-radius:8px;
  padding:12px 16px;font-family:'JetBrains Mono',monospace;font-size:.74rem;
  max-height:240px;overflow-y:auto;color:#94a3b8;}
.logbox .ok{color:#34d399;} .logbox .err{color:#f87171;}
.logbox .warn{color:#fbbf24;} .logbox .act{color:#60a5fa;}

.stats{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin-bottom:14px;}
.stat{background:#fff;border:1px solid #dce6f5;border-radius:8px;
  padding:12px;text-align:center;}
.stat-v{font-size:1.7rem;font-weight:800;color:#1a4fad;line-height:1;}
.stat-l{font-size:.72rem;color:#64748b;margin-top:3px;font-weight:500;}

.login-box{background:#fffbeb;border:1px solid #fbbf24;border-radius:10px;
  padding:20px 24px;margin:12px 0;}
.login-box h3{color:#92400e;margin:0 0 8px 0;font-size:1rem;}
.login-box p{color:#78350f;font-size:.87rem;margin:0;}

.stButton>button{background:#1a4fad!important;color:#fff!important;
  border:none!important;border-radius:8px!important;font-weight:600!important;
  padding:9px 22px!important;}
.stButton>button:hover{background:#1d6fdb!important;}
.stTextInput>div>div>input,.stTextArea>div>div>textarea{
  background:#f8faff!important;border:1px solid #dce6f5!important;
  border-radius:7px!important;color:#1e293b!important;}
label{color:#1e293b!important;}
.footer{text-align:center;font-size:.72rem;color:#94a3b8;margin-top:24px;
  padding-top:12px;border-top:1px solid #dce6f5;}
</style>
""", unsafe_allow_html=True)


# =============================================================================
# SECTION 2: LOGGING
# =============================================================================
class LogStore:
    """Thread-safe ring buffer of log records, mirrored to a real log file on disk."""

    def __init__(self, max_records: int = 2000, log_dir: Optional[Path] = None) -> None:
        self._lock = threading.Lock()
        self._records: deque[tuple[str, str]] = deque(maxlen=max_records)
        self.log_dir = log_dir or (Path.home() / "PortalAutomationLogs")
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.log_file = self.log_dir / f"automation_{datetime.now():%Y%m%d_%H%M%S}.log"

        if LOGURU_AVAILABLE:
            _loguru_logger.remove()
            _loguru_logger.add(str(self.log_file), rotation="5 MB", retention=5, level="DEBUG")
            self._backend = _loguru_logger
        else:
            import logging as _logging
            self._backend = _logging.getLogger("portal_automation")
            self._backend.setLevel(_logging.DEBUG)
            handler = _logging.FileHandler(self.log_file, encoding="utf-8")
            handler.setFormatter(_logging.Formatter("%(asctime)s | %(levelname)s | %(message)s"))
            self._backend.addHandler(handler)

    def _write(self, kind: str, message: str) -> None:
        ts = datetime.now().strftime("%H:%M:%S")
        with self._lock:
            self._records.append((f"[{ts}] {message}", kind))
        level = {"ok": "success", "err": "error", "warn": "warning", "act": "info", "info": "info"}.get(kind, "info")
        try:
            getattr(self._backend, level, self._backend.info)(message)
        except Exception:
            pass

    def info(self, m: str) -> None: self._write("info", m)
    def act(self, m: str) -> None: self._write("act", m)
    def warning(self, m: str) -> None: self._write("warn", m)
    def error(self, m: str) -> None: self._write("err", m)
    def success(self, m: str) -> None: self._write("ok", m)

    def records(self) -> list[tuple[str, str]]:
        with self._lock:
            return list(self._records)

    def render_html(self, tail: int = 100) -> str:
        recs = self.records()[-tail:]
        html = '<div class="logbox">'
        if not recs:
            html += '<span class="warn">Waiting…</span>'
        for txt, kind in reversed(recs):
            html += f'<div class="{kind}">{txt}</div>'
        return html + "</div>"


# =============================================================================
# SECTION 3: DATA MODELS
# =============================================================================
@dataclass
class SearchResultItem:
    """One row/card/tile found in the portal's result set for a given search term."""
    index: int
    label: str
    source_term: str
    raw_text: str = ""
    library: str = ""
    selected: bool = False


TEMP_DOWNLOAD_SUFFIXES = (".crdownload", ".tmp", ".partial", ".download")


def safe_filename(name: str) -> str:
    name = unicodedata.normalize("NFKD", name)
    name = re.sub(r'[\\/:*?"<>|]', "_", name).strip()
    return name or "download"


def dedupe_path(target: Path) -> Path:
    if not target.exists():
        return target
    stem, suffix, parent = target.stem, target.suffix, target.parent
    counter = 1
    while True:
        candidate = parent / f"{stem} ({counter}){suffix}"
        if not candidate.exists():
            return candidate
        counter += 1


# =============================================================================
# SECTION 4: RETRY MANAGER
# =============================================================================
class RetryManager:
    def __init__(self, log: LogStore, max_attempts: int = 3, base_delay: float = 0.75):
        self.log = log
        self.max_attempts = max_attempts
        self.base_delay = base_delay

    def run(self, fn: Callable[[], Any], description: str, swallow: bool = False) -> Any:
        last_exc: Optional[Exception] = None
        for attempt in range(1, self.max_attempts + 1):
            try:
                return fn()
            except Exception as exc:  # noqa: BLE001 - self-healing boundary
                last_exc = exc
                self.log.warning(f"Attempt {attempt}/{self.max_attempts} failed for '{description}': {exc}")
                if attempt < self.max_attempts:
                    time.sleep(self.base_delay * (2 ** (attempt - 1)))
        if swallow:
            self.log.error(f"'{description}' failed after {self.max_attempts} attempts (continuing): {last_exc}")
            return None
        raise RuntimeError(f"'{description}' failed after {self.max_attempts} attempts: {last_exc}") from last_exc


# =============================================================================
# SECTION 5: OCR HELPER (tesseract CLI) + NATIVE DIALOG HELPER
# =============================================================================
class OCRHelper:
    """Shells out to the `tesseract` CLI (no pytesseract wrapper available) for image-only controls."""

    def __init__(self, log: LogStore):
        self.log = log

    @property
    def available(self) -> bool:
        return TESSERACT_AVAILABLE

    def find_text_coordinates(self, screenshot_bytes: bytes, target_text: str) -> Optional[tuple[int, int]]:
        if not self.available:
            return None
        target_norm = target_text.strip().lower()
        if not target_norm:
            return None
        with tempfile.TemporaryDirectory() as tmp_dir:
            image_path = Path(tmp_dir) / "screenshot.png"
            image_path.write_bytes(screenshot_bytes)
            output_base = Path(tmp_dir) / "ocr_output"
            try:
                subprocess.run(
                    ["tesseract", str(image_path), str(output_base), "tsv"],
                    check=True, capture_output=True, timeout=20,
                )
            except Exception as exc:
                self.log.warning(f"tesseract CLI invocation failed: {exc}")
                return None
            tsv_path = output_base.with_suffix(".tsv")
            if not tsv_path.exists():
                return None
            try:
                with open(tsv_path, "r", encoding="utf-8", errors="ignore") as handle:
                    for row in csv.DictReader(handle, delimiter="\t"):
                        word = (row.get("text") or "").strip()
                        if word and target_norm in word.lower():
                            left, top = int(row["left"]), int(row["top"])
                            width, height = int(row["width"]), int(row["height"])
                            return (left + width // 2, top + height // 2)
            except Exception as exc:
                self.log.warning(f"Failed parsing tesseract TSV output: {exc}")
        return None


class NativeDialogHelper:
    """Optional last-resort helper for native OS 'Save As' dialogs, active only if pyautogui is installed."""

    def __init__(self, log: LogStore):
        self.log = log

    @property
    def available(self) -> bool:
        return PYAUTOGUI_AVAILABLE

    def accept_default_save(self) -> None:
        if not self.available:
            return
        try:
            pyautogui.press("enter")
        except Exception as exc:
            self.log.error(f"NativeDialogHelper could not send Enter: {exc}")

    def confirm_print_and_save(self, presses: int = 2, delay: float = 1.5) -> None:
        """
        Print icons open a native OS print dialog (invisible to the DOM),
        and choosing 'Save as PDF' there can chain into a second native
        Save-As dialog. Both are simple confirm-with-Enter flows in the
        common case, so this sends up to `presses` Enter keystrokes spaced
        `delay` seconds apart - enough for either a single dialog or the
        two-dialog chain, without assuming which one occurred.
        """
        if not self.available:
            self.log.warning(
                "pyautogui is not installed - can't confirm the native print dialog automatically. "
                "Please accept it manually in the Edge window."
            )
            return
        for _ in range(presses):
            try:
                pyautogui.press("enter")
            except Exception as exc:
                self.log.error(f"NativeDialogHelper could not send Enter: {exc}")
                return
            time.sleep(delay)


# =============================================================================
# SECTION 6: LOCATOR HEURISTICS
# =============================================================================
class LocatorHints:
    SEARCH_KEYWORDS = ["search", "filter", "find", "lookup", "query", "keyword"]
    FOLDER_KEYWORDS = ["folder", "directory", "category", "workspace", "repository", "library"]
    DOWNLOAD_KEYWORDS = ["download", "export", "save", "get file", "retrieve"]
    PRINT_KEYWORDS = ["print"]
    SPINNER_SELECTORS = [
        "mat-spinner", "mat-progress-bar", "mat-progress-spinner",
        "[class*='spinner']", "[class*='loading']", "[class*='loader']",
        "p-progressspinner", "[role='progressbar']", "ngx-spinner",
    ]

    @staticmethod
    def search_box_css_candidates() -> list[str]:
        candidates = []
        for kw in LocatorHints.SEARCH_KEYWORDS:
            candidates += [
                f"input[placeholder*='{kw}' i]", f"input[aria-label*='{kw}' i]",
                f"input[name*='{kw}' i]", f"input[id*='{kw}' i]",
                f"[data-testid*='{kw}' i] input", f"input[data-testid*='{kw}' i]",
            ]
        candidates += ["input[type='search']", "input[role='searchbox']"]
        return candidates

    @staticmethod
    def download_button_css_candidates() -> list[str]:
        candidates = []
        for kw in LocatorHints.DOWNLOAD_KEYWORDS:
            candidates += [
                f"button[aria-label*='{kw}' i]", f"a[aria-label*='{kw}' i]",
                f"[data-testid*='{kw}' i]", f"button[title*='{kw}' i]",
                f"a[title*='{kw}' i]", f"[class*='{kw}' i]", f"svg[aria-label*='{kw}' i]",
            ]
        return candidates

    @staticmethod
    def download_button_xpath_candidates() -> list[str]:
        candidates = []
        upper, lower = "ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz"
        for kw in LocatorHints.DOWNLOAD_KEYWORDS:
            for tag in ("button", "a", "mat-icon", "span"):
                candidates.append(
                    f"//{tag}[contains(translate(normalize-space(.), '{upper}', '{lower}'), '{kw}')]"
                )
        return candidates

    @staticmethod
    def print_control_candidates() -> list[str]:
        """
        Some document viewers expose no direct 'download' control at all -
        the only way to obtain a copy is the print icon (which opens the
        browser's native print dialog, from which 'Save as PDF' produces a
        file). These candidates locate that icon the same heuristic way as
        every other control in this file.
        """
        candidates = []
        upper, lower = "ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz"
        for kw in LocatorHints.PRINT_KEYWORDS:
            candidates += [
                f"button[aria-label*='{kw}' i]", f"a[aria-label*='{kw}' i]",
                f"[data-testid*='{kw}' i]", f"button[title*='{kw}' i]",
                f"a[title*='{kw}' i]", f"[class*='{kw}' i]", f"svg[aria-label*='{kw}' i]",
            ]
            for tag in ("button", "a", "mat-icon", "span"):
                candidates.append(
                    f"//{tag}[contains(translate(normalize-space(.), '{upper}', '{lower}'), '{kw}')]"
                )
        return candidates


def pick_best_search_input(html: str) -> Optional[str]:
    """
    Score every <input> in the page's live, already-authenticated HTML
    (fetched by Selenium - never a separate `requests` call, so no session
    cookies ever leave the browser) against search-related signals, and
    return a precise CSS selector for the single best match rather than
    trying a long list of generic guesses first. Falls back to None if
    BeautifulSoup can't find a confident match, so the caller can drop
    back to the broader heuristic scan.
    """
    if not BS4_AVAILABLE:
        return None
    soup = BeautifulSoup(html, _bs4_parser())
    best_score, best_selector = 0, None
    for inp in soup.find_all("input"):
        attrs_text = " ".join(str(inp.get(a, "")) for a in
                               ("placeholder", "aria-label", "name", "id", "title", "data-testid")).lower()
        score = sum(2 for kw in LocatorHints.SEARCH_KEYWORDS if kw in attrs_text)
        if (inp.get("type") or "").lower() == "search":
            score += 3
        if (inp.get("role") or "").lower() == "searchbox":
            score += 3
        if score <= 0 or score <= best_score:
            continue
        best_score = score
        if inp.get("id"):
            best_selector = f"#{inp['id']}"
        elif inp.get("data-testid"):
            best_selector = f"input[data-testid='{inp['data-testid']}']"
        elif inp.get("name"):
            best_selector = f"input[name='{inp['name']}']"
    return best_selector


# =============================================================================
# SECTION 7: BROWSER ADAPTER (Selenium - real DOM interaction only)
# =============================================================================
class BrowserAdapter(ABC):
    engine_name: str = "unknown"

    @abstractmethod
    def navigate(self, url: str) -> None: ...
    @abstractmethod
    def current_url(self) -> str: ...
    @abstractmethod
    def page_html(self) -> str: ...
    @abstractmethod
    def find_first(self, locator_candidates: list[str]) -> Optional[Any]: ...
    @abstractmethod
    def click(self, element: Any) -> None: ...
    @abstractmethod
    def type_text(self, element: Any, text: str) -> None: ...
    @abstractmethod
    def press_enter(self, element: Any) -> None: ...
    @abstractmethod
    def scroll_down(self, pixels: int = 2000) -> None: ...
    @abstractmethod
    def wait_network_idle(self, timeout_ms: int = 8000) -> None: ...
    @abstractmethod
    def wait_no_spinner(self, timeout_ms: int = 10000) -> None: ...
    @abstractmethod
    def wait_dom_stable(self, timeout_ms: int = 6000) -> None: ...
    @abstractmethod
    def screenshot_bytes(self) -> bytes: ...
    @abstractmethod
    def click_at(self, x: int, y: int) -> None: ...
    @abstractmethod
    def download_via_click(self, element: Any, timeout_s: float = 30.0) -> Optional[Path]: ...
    @abstractmethod
    def download_via_print_click(self, element: Any, timeout_s: float = 30.0) -> Optional[Path]: ...
    @abstractmethod
    def minimize_to_corner(self, width: int = 420, height: int = 320) -> None: ...
    @abstractmethod
    def restore_size(self) -> None: ...
    @abstractmethod
    def close(self) -> None: ...


class _DownloadEventHandler(FileSystemEventHandler):
    def __init__(self) -> None:
        super().__init__()
        self.completed_queue: "queue.Queue[Path]" = queue.Queue()

    def _maybe_report(self, path_str: str) -> None:
        path = Path(path_str)
        if path.suffix.lower() not in TEMP_DOWNLOAD_SUFFIXES:
            self.completed_queue.put(path)

    def on_moved(self, event):
        if not event.is_directory:
            self._maybe_report(event.dest_path)

    def on_created(self, event):
        if not event.is_directory:
            self._maybe_report(event.src_path)


class DownloadWatcher:
    """watchdog-based (event-driven) completion detector; polling fallback if unavailable."""

    def __init__(self, target_dir: Path):
        self.target_dir = target_dir
        self._handler = _DownloadEventHandler() if WATCHDOG_AVAILABLE else None
        self._observer = None
        if WATCHDOG_AVAILABLE:
            self._observer = Observer()
            self._observer.schedule(self._handler, str(target_dir), recursive=False)
            self._observer.start()

    def wait_for_new_file(self, timeout: float = 30.0) -> Optional[Path]:
        if self._observer is not None:
            try:
                path = self._handler.completed_queue.get(timeout=timeout)
                time.sleep(0.3)
                return path
            except queue.Empty:
                return None
        deadline = time.time() + timeout
        before = {p.name for p in self.target_dir.glob("*")}
        while time.time() < deadline:
            current = {p.name for p in self.target_dir.glob("*") if p.suffix.lower() not in TEMP_DOWNLOAD_SUFFIXES}
            new_files = current - before
            if new_files:
                return max((self.target_dir / n for n in new_files), key=lambda p: p.stat().st_mtime)
            time.sleep(0.4)
        return None

    def stop(self) -> None:
        if self._observer is not None:
            self._observer.stop()
            self._observer.join(timeout=2)


class SeleniumAdapter(BrowserAdapter):
    """The sole browser engine: drives a real, visible Edge window via Selenium only."""

    engine_name = "Selenium (Edge)"

    def __init__(self, log: LogStore, download_dir: Path):
        if not SELENIUM_AVAILABLE:
            raise RuntimeError("Selenium is not installed in this environment.")
        self.log = log
        self.download_dir = download_dir
        download_dir.mkdir(parents=True, exist_ok=True)

        options = EdgeOptions()
        options.use_chromium = True
        options.add_experimental_option("prefs", {
            "download.default_directory": str(download_dir),
            "download.prompt_for_download": False,
            "safebrowsing.enabled": True,
        })
        self.driver = webdriver.Edge(options=options)  # selenium-manager auto-resolves msedgedriver
        self.driver.maximize_window()
        self.native_dialog_helper = NativeDialogHelper(log)
        self._download_watcher = DownloadWatcher(download_dir)

    def navigate(self, url: str) -> None:
        self.driver.get(url)

    def current_url(self) -> str:
        return self.driver.current_url

    def page_html(self) -> str:
        return self.driver.page_source

    def find_first(self, locator_candidates: list[str]) -> Optional[Any]:
        for candidate in locator_candidates:
            try:
                by = By.XPATH if candidate.strip().startswith(("//", "(//", ".//")) else By.CSS_SELECTOR
                for el in self.driver.find_elements(by, candidate):
                    if el.is_displayed():
                        return el
            except Exception:
                continue
        return None

    def click(self, element: Any) -> None:
        element.click()

    def type_text(self, element: Any, text: str) -> None:
        element.clear()
        element.send_keys(text)

    def press_enter(self, element: Any) -> None:
        element.send_keys(Keys.ENTER)

    def scroll_down(self, pixels: int = 2000) -> None:
        try:
            self.driver.execute_script("window.scrollBy(0, arguments[0]);", pixels)
        except Exception:
            pass

    def click_at(self, x: int, y: int) -> None:
        script = """
        (function(x, y) {
            var el = document.elementFromPoint(x, y);
            if (!el) { return false; }
            ['mousedown', 'mouseup', 'click'].forEach(function(type) {
                var ev = new MouseEvent(type, {view: window, bubbles: true, cancelable: true, clientX: x, clientY: y});
                el.dispatchEvent(ev);
            });
            return true;
        })(arguments[0], arguments[1]);
        """
        self.driver.execute_script(script, x, y)

    def wait_network_idle(self, timeout_ms: int = 8000) -> None:
        try:
            WebDriverWait(self.driver, timeout_ms / 1000).until(
                lambda d: d.execute_script("return document.readyState") == "complete"
            )
        except SeleniumTimeoutException:
            pass

    def wait_no_spinner(self, timeout_ms: int = 10000) -> None:
        deadline = time.time() + (timeout_ms / 1000)
        selector = ", ".join(LocatorHints.SPINNER_SELECTORS)
        while time.time() < deadline:
            try:
                els = self.driver.find_elements(By.CSS_SELECTOR, selector)
                if not any(e.is_displayed() for e in els):
                    return
            except Exception:
                return
            time.sleep(0.25)

    def wait_dom_stable(self, timeout_ms: int = 6000) -> None:
        deadline = time.time() + (timeout_ms / 1000)
        last_len, stable_reads = -1, 0
        while time.time() < deadline:
            try:
                length = self.driver.execute_script("return document.body.innerHTML.length")
            except Exception:
                return
            if length == last_len:
                stable_reads += 1
                if stable_reads >= 3:
                    return
            else:
                stable_reads = 0
            last_len = length
            time.sleep(0.2)

    def screenshot_bytes(self) -> bytes:
        return self.driver.get_screenshot_as_png()

    def download_via_click(self, element: Any, timeout_s: float = 30.0) -> Optional[Path]:
        element.click()
        result = self._download_watcher.wait_for_new_file(timeout=min(timeout_s, 8.0))
        if result is None and self.native_dialog_helper.available:
            self.native_dialog_helper.accept_default_save()
            result = self._download_watcher.wait_for_new_file(timeout=max(timeout_s - 8.0, 5.0))
        if result:
            final_path = dedupe_path(result.parent / safe_filename(result.name))
            if final_path != result:
                result.rename(final_path)
            return final_path
        return None

    def download_via_print_click(self, element: Any, timeout_s: float = 30.0) -> Optional[Path]:
        # Print dialogs are native OS windows with no DOM presence, so
        # (unlike download_via_click) we go straight to the native-dialog
        # helper right after the click rather than watching first.
        element.click()
        time.sleep(1.2)  # let the native print dialog render before we act on it
        self.native_dialog_helper.confirm_print_and_save()
        result = self._download_watcher.wait_for_new_file(timeout=timeout_s)
        if result:
            final_path = dedupe_path(result.parent / safe_filename(result.name))
            if final_path != result:
                result.rename(final_path)
            return final_path
        return None

    def minimize_to_corner(self, width: int = 420, height: int = 320) -> None:
        """Shrinks the automation window once login is done, so Streamlit stays the visual focus."""
        try:
            x = self.driver.execute_script("return window.screen.availWidth;") - width - 20
            y = self.driver.execute_script("return window.screen.availHeight;") - height - 60
            self.driver.set_window_rect(x=max(x, 0), y=max(y, 0), width=width, height=height)
        except Exception as exc:
            self.log.warning(f"Could not resize the automation window: {exc}")

    def restore_size(self) -> None:
        try:
            self.driver.set_window_rect(x=40, y=40, width=1280, height=860)
        except Exception as exc:
            self.log.warning(f"Could not restore the automation window: {exc}")

    def close(self) -> None:
        try:
            self._download_watcher.stop()
        finally:
            self.driver.quit()


class BrowserManager:
    def __init__(self, log: LogStore, download_dir: Path):
        self.log = log
        self.download_dir = download_dir
        self.adapter: Optional[BrowserAdapter] = None

    def launch(self, max_attempts: int = 3) -> BrowserAdapter:
        if not SELENIUM_AVAILABLE:
            raise RuntimeError("Selenium is not installed. Install it with `pip install selenium`.")
        last_exc: Optional[Exception] = None
        for attempt in range(1, max_attempts + 1):
            try:
                self.log.act(f"Launching Microsoft Edge via Selenium (attempt {attempt}/{max_attempts})...")
                self.adapter = SeleniumAdapter(self.log, self.download_dir)
                self.log.success("Edge session started.")
                return self.adapter
            except Exception as exc:
                last_exc = exc
                self.log.warning(f"Edge launch attempt {attempt} failed: {exc}")
                time.sleep(1.5)
        raise RuntimeError(f"Failed to launch Microsoft Edge after {max_attempts} attempts: {last_exc}") from last_exc

    def close(self) -> None:
        if self.adapter:
            try:
                self.adapter.close()
            except Exception as exc:
                self.log.warning(f"Error while closing browser: {exc}")
            finally:
                self.adapter = None


# =============================================================================
# SECTION 8: LOCATOR / ANGULAR / TABLE ENGINES
# =============================================================================
class LocatorEngine:
    def __init__(self, adapter: BrowserAdapter, log: LogStore, retry: RetryManager, ocr: OCRHelper):
        self.adapter = adapter
        self.log = log
        self.retry = retry
        self.ocr = ocr

    def find_search_box(self) -> Optional[Any]:
        # First choice: parse the page's live, already-authenticated HTML
        # with BeautifulSoup and target the single best-scoring <input>
        # precisely, instead of trying many generic selectors blind.
        try:
            precise_selector = pick_best_search_input(self.adapter.page_html())
        except Exception:
            precise_selector = None
        if precise_selector:
            element = self.retry.run(
                lambda: self.adapter.find_first([precise_selector]),
                "locate search box (HTML-parsed precise match)", swallow=True,
            )
            if element is not None:
                return element

        # Self-healing fallback: broader heuristic scan.
        element = self.retry.run(
            lambda: self.adapter.find_first(LocatorHints.search_box_css_candidates()),
            "locate search box", swallow=True,
        )
        if element is None:
            self.log.warning("No search box found via DOM heuristics.")
        return element

    def find_download_controls(self) -> list[Any]:
        found = []
        for candidate in LocatorHints.download_button_css_candidates() + LocatorHints.download_button_xpath_candidates():
            try:
                el = self.adapter.find_first([candidate])
                if el is not None:
                    found.append(el)
            except Exception:
                continue
        return found

    def find_print_control(self) -> Optional[Any]:
        """Locate a print icon/button as a fallback delivery mechanism when no direct download control exists."""
        return self.retry.run(
            lambda: self.adapter.find_first(LocatorHints.print_control_candidates()),
            "locate print control", swallow=True,
        )

    def find_download_control_via_ocr(self) -> Optional[tuple[int, int]]:
        if not self.ocr.available:
            return None
        try:
            shot = self.adapter.screenshot_bytes()
        except Exception:
            return None
        for kw in LocatorHints.DOWNLOAD_KEYWORDS:
            coords = self.ocr.find_text_coordinates(shot, kw)
            if coords:
                self.log.info(f"OCR located a '{kw}' control at {coords}.")
                return coords
        return None

    def discover_navigation(self) -> list[str]:
        html = self.adapter.page_html()
        labels: list[str] = []
        if BS4_AVAILABLE:
            soup = BeautifulSoup(html, _bs4_parser())
            for el in soup.select(
                "nav a, [role='treeitem'], [role='menuitem'], [role='navigation'] a, "
                "mat-tree-node, p-treenode, li[class*='nav'], a[class*='folder'], div[class*='folder']"
            ):
                text = el.get_text(strip=True) or el.get("aria-label", "") or el.get("title", "")
                if text and len(text) < 120:
                    labels.append(text)
        seen: set[str] = set()
        unique = []
        for label in labels:
            if label not in seen:
                seen.add(label)
                unique.append(label)
        return unique[:200]


class AngularEngine:
    def __init__(self, adapter: BrowserAdapter, log: LogStore):
        self.adapter = adapter
        self.log = log

    def wait_stable(self, network_timeout_ms: int = 8000, spinner_timeout_ms: int = 12000,
                     dom_timeout_ms: int = 6000) -> None:
        self.adapter.wait_network_idle(network_timeout_ms)
        self.adapter.wait_no_spinner(spinner_timeout_ms)
        self.adapter.wait_dom_stable(dom_timeout_ms)


class TableDetector:
    ROW_SELECTORS = [
        "table tbody tr",
        "div.ag-center-cols-container div[role='row']",
        "p-table tr",
        "[role='grid'] [role='row']",
        "[class*='result'] [class*='row']",
        "[class*='result-item']",
        "[class*='card']",
    ]

    def __init__(self, adapter: BrowserAdapter, log: LogStore):
        self.adapter = adapter
        self.log = log

    def extract_results(self, source_term: str) -> list[SearchResultItem]:
        if not BS4_AVAILABLE:
            self.log.warning("BeautifulSoup not installed - cannot parse result rows.")
            return []
        soup = BeautifulSoup(self.adapter.page_html(), _bs4_parser())
        items: list[SearchResultItem] = []
        seen: set[str] = set()
        for selector in self.ROW_SELECTORS:
            for row in soup.select(selector):
                text = row.get_text(" ", strip=True)
                if not text or text in seen or len(text) > 500:
                    continue
                seen.add(text)
                items.append(SearchResultItem(index=len(items), label=text[:160], source_term=source_term, raw_text=text))
            if items:
                break  # first matching selector family wins - avoids double counting one grid
        return items


class FolderNavigator:
    """Opens a named folder/category in the portal before searching (optional, user-typed name)."""

    def __init__(self, adapter: BrowserAdapter, angular_engine: AngularEngine, log: LogStore):
        self.adapter = adapter
        self.angular_engine = angular_engine
        self.log = log

    def open_folder(self, folder_label: str) -> bool:
        try:
            escaped = folder_label.replace("'", "\\'")
            element = self.adapter.find_first([f"//*[contains(normalize-space(text()), '{escaped}')]"])
            if element is None:
                self.log.warning(f"Could not locate folder '{folder_label}'.")
                return False
            self.adapter.click(element)
            self.angular_engine.wait_stable()
            self.log.success(f"Opened folder: {folder_label}")
            return True
        except Exception as exc:
            self.log.error(f"Failed opening folder '{folder_label}': {exc}")
            return False


# =============================================================================
# SECTION 9: SEARCH + DOWNLOAD MANAGERS
# =============================================================================
class SearchManager:
    def __init__(self, adapter: BrowserAdapter, locator_engine: LocatorEngine,
                 angular_engine: AngularEngine, table_detector: TableDetector,
                 retry: RetryManager, log: LogStore):
        self.adapter = adapter
        self.locator_engine = locator_engine
        self.angular_engine = angular_engine
        self.table_detector = table_detector
        self.retry = retry
        self.log = log

    def search(self, term: str) -> list[SearchResultItem]:
        search_box = self.locator_engine.find_search_box()
        if search_box is None:
            raise RuntimeError("No search box could be located on this page.")
        self.retry.run(lambda: self.adapter.type_text(search_box, term), f"type '{term}'")
        self.retry.run(lambda: self.adapter.press_enter(search_box), f"submit '{term}'")
        self.angular_engine.wait_stable()
        self._scroll_for_lazy_load()
        return self.table_detector.extract_results(term)

    def _scroll_for_lazy_load(self, max_scrolls: int = 5) -> None:
        for _ in range(max_scrolls):
            before = len(self.table_detector.extract_results(""))
            self.adapter.scroll_down(2000)
            self.angular_engine.wait_stable(dom_timeout_ms=2500)
            after = len(self.table_detector.extract_results(""))
            if after <= before:
                break


class DownloadManager:
    """
    Clicks the download control visible after (re-)searching a term.

    KNOWN LIMITATION (documented rather than hidden): the DOM-heuristic
    locator finds the first visible download control on the page, not a
    control tied precisely to one specific row. This matches what's
    generically detectable without portal-specific selectors. If a term's
    results contain more than one document, downloading a specific one of
    them reliably requires the person to narrow the search term (e.g. an
    exact document number) so it returns a single match.
    """

    def __init__(self, adapter: BrowserAdapter, locator_engine: LocatorEngine,
                 retry: RetryManager, log: LogStore, download_dir: Path):
        self.adapter = adapter
        self.locator_engine = locator_engine
        self.retry = retry
        self.log = log
        self.download_dir = download_dir
        self.download_dir.mkdir(parents=True, exist_ok=True)

    def download_first_visible(self, label_for_log: str) -> Optional[Path]:
        # Strategy 1: a direct download control (button/link/icon).
        controls = self.locator_engine.find_download_controls()
        if controls:
            saved = self.retry.run(lambda: self.adapter.download_via_click(controls[0]), f"download '{label_for_log}'")
            if saved:
                self.log.success(f"Downloaded: {saved.name}")
            return saved

        # Strategy 2: some viewers only expose a print icon; printing to
        # 'Save as PDF' is a legitimate, common way such portals hand out a
        # copy of a document without a dedicated download button.
        print_control = self.locator_engine.find_print_control()
        if print_control is not None:
            self.log.info(f"No direct download control found for '{label_for_log}'; trying the print-to-save workflow.")
            saved = self.retry.run(
                lambda: self.adapter.download_via_print_click(print_control),
                f"print-download '{label_for_log}'",
            )
            if saved:
                self.log.success(f"Downloaded via print workflow: {saved.name}")
            return saved

        # Strategy 3 (last resort): OCR the viewport for an image-only control.
        coords = self.locator_engine.find_download_control_via_ocr()
        if coords:
            self.log.info("Using OCR-detected coordinates as last-resort download trigger.")
            self.adapter.click_at(*coords)
            self.log.warning("OCR-triggered downloads cannot be positively confirmed; check the folder manually.")
            return None

        raise RuntimeError(f"No download, print, or image-based control found for '{label_for_log}'.")


# =============================================================================
# SECTION 10: INPUT PROCESSOR
# =============================================================================
class InputProcessor:
    @staticmethod
    def from_manual(text: str) -> list[str]:
        return [t.strip() for t in re.split(r"[,\n]", text) if t.strip()]

    @staticmethod
    def from_excel(uploaded_file) -> list[str]:
        df = pd.read_excel(uploaded_file, engine="openpyxl")
        col = df.columns[0]
        skip_headers = {"number", "part", "search", "document", "doc", "ref", "keyword"}
        values = [str(v).strip() for v in df[col].dropna().tolist() if str(v).strip()]
        if values and values[0].lower() in skip_headers:
            values = values[1:]
        return values


# =============================================================================
# SECTION 11: SHARED STATE + BACKGROUND WORKERS
# =============================================================================
class SharedSearchState:
    """Thread-safe bridge for the background search worker."""
    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.results: list[SearchResultItem] = []
        self.running = False
        self.done = False


def search_worker(adapter: BrowserAdapter, log: LogStore, terms: list[str],
                   folder_label: Optional[str], shared: SharedSearchState) -> None:
    shared.running = True
    retry = RetryManager(log)
    ocr = OCRHelper(log)
    locator_engine = LocatorEngine(adapter, log, retry, ocr)
    angular_engine = AngularEngine(adapter, log)
    table_detector = TableDetector(adapter, log)
    search_manager = SearchManager(adapter, locator_engine, angular_engine, table_detector, retry, log)

    if folder_label:
        log.act(f"Opening folder '{folder_label}' before searching...")
        FolderNavigator(adapter, angular_engine, log).open_folder(folder_label)

    all_results: list[SearchResultItem] = []
    for term in terms:
        log.act(f"Searching: '{term}'")
        try:
            results = search_manager.search(term)
            log.success(f"'{term}': {len(results)} result(s) found.")
            all_results.extend(results)
        except Exception as exc:
            log.error(f"Search failed for '{term}': {exc}")

    with shared.lock:
        shared.results = all_results
    shared.running = False
    shared.done = True
    log.success(f"Search phase complete - {len(all_results)} total result(s) across {len(terms)} term(s).")


class SharedDownloadState:
    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.saved_paths: list[str] = []
        self.completed = 0
        self.total = 0
        self.running = False
        self.done = False


def download_worker(adapter: BrowserAdapter, log: LogStore, selected: list[SearchResultItem],
                     download_dir: Path, shared: SharedDownloadState) -> None:
    shared.running = True
    shared.total = len(selected)
    retry = RetryManager(log)
    ocr = OCRHelper(log)
    locator_engine = LocatorEngine(adapter, log, retry, ocr)
    angular_engine = AngularEngine(adapter, log)
    table_detector = TableDetector(adapter, log)
    search_manager = SearchManager(adapter, locator_engine, angular_engine, table_detector, retry, log)
    download_manager = DownloadManager(adapter, locator_engine, retry, log, download_dir)

    by_term: dict[str, list[SearchResultItem]] = {}
    for item in selected:
        by_term.setdefault(item.source_term, []).append(item)

    for term, items in by_term.items():
        log.act(f"Re-opening results for '{term}' to download {len(items)} item(s)...")
        try:
            search_manager.search(term)
        except Exception as exc:
            log.error(f"Could not re-open '{term}': {exc}")
            continue
        for item in items:
            try:
                saved = download_manager.download_first_visible(item.label[:60])
                if saved:
                    with shared.lock:
                        shared.saved_paths.append(str(saved))
            except Exception as exc:
                log.error(f"Download failed for '{item.label[:60]}': {exc}")
            finally:
                with shared.lock:
                    shared.completed += 1

    shared.running = False
    shared.done = True
    log.success(f"Download phase complete - {len(shared.saved_paths)}/{shared.total} file(s) saved.")


# =============================================================================
# SECTION 12: UI HELPERS (branded chrome)
# =============================================================================
STEP_NAMES = ["Setup", "Login", "Search", "Select", "Download"]


def render_header(brand_name: str, brand_subtitle: str) -> None:
    chip = "".join(w[0] for w in brand_name.split()[:2]).upper() or "EP"
    st.markdown(f"""
    <div class="hdr">
      <span class="logo-chip">{chip}</span>
      <div class="hdr-txt">
        <h1>{brand_name} — Document Hunter</h1>
        <p>{brand_subtitle}</p>
      </div>
    </div>
    """, unsafe_allow_html=True)


def render_step_bar(current: int) -> None:
    html = '<div class="steps">'
    for i, name in enumerate(STEP_NAMES, 1):
        cls = "active" if i == current else ("done" if i < current else "step")
        icon = "✓" if i < current else str(i)
        html += f'<div class="step {cls}"><span class="snum">{icon}</span>{name}</div>'
    st.markdown(html + "</div>", unsafe_allow_html=True)


def ext_of(label: str) -> str:
    m = re.search(r"\.(pdf|docx?|xlsx?|zip|csv|pptx?|txt)\b", label, re.I)
    return f".{m.group(1).lower()}" if m else ""


# =============================================================================
# SECTION 13: SESSION STATE
# =============================================================================
def init_session_state() -> None:
    defaults = {
        "step": 1,
        "brand_name": "Your Company",
        "brand_subtitle": "Enterprise document portal automation · no hardcoded portals",
        "portal_url": "",
        "search_terms": [],
        "folder_label": "",
        "folder_decision": None,     # None -> "pending" -> "resolved" (see render_folder_gate)
        "available_folders": [],
        "download_dir": str(Path.home() / "Downloads" / "PortalDocs"),
        "log_store": LogStore(),
        "browser_manager": None,
        "search_shared": None,
        "download_shared": None,
        "results": [],
    }
    for k, v in defaults.items():
        if k not in st.session_state:
            st.session_state[k] = v


def full_reset() -> None:
    bm: Optional[BrowserManager] = st.session_state.get("browser_manager")
    if bm:
        bm.close()
    for key in list(st.session_state.keys()):
        del st.session_state[key]
    st.rerun()


# =============================================================================
# SECTION 14: STEP RENDERERS
# =============================================================================
def render_step_1() -> None:
    S = st.session_state
    st.markdown('<div class="card"><div class="card-title"><div class="dot"></div>Portal &amp; Search Setup</div>', unsafe_allow_html=True)

    with st.expander("🎨 Customize appearance (optional)"):
        S.brand_name = st.text_input("Display name", value=S.brand_name)
        S.brand_subtitle = st.text_input("Subtitle", value=S.brand_subtitle)

    c1, c2 = st.columns([3, 2])
    with c1:
        url_val = st.text_input("🌐 Portal URL", value=S.portal_url, placeholder="https://portal.example.com")
    with c2:
        dl_val = st.text_input("📁 Download folder", value=S.download_dir)

    terms_raw = st.text_area("🔎 Search terms — comma or newline separated",
                              placeholder="BRG1274, BRG5678, 72-32-55-300-001", height=80)
    st.caption("You'll be asked whether to scope the search to a specific folder after you log in.")

    with st.expander("📤 Or upload an Excel batch (first column = search terms)"):
        xl = st.file_uploader("Upload .xlsx", type=["xlsx", "xlsm"])
        if xl:
            try:
                batch = InputProcessor.from_excel(xl)
                st.success(f"✅ {len(batch)} term(s) loaded from Excel")
                terms_raw = ", ".join(batch)
            except Exception as exc:
                st.error(f"Excel error: {exc}")

    st.markdown("</div>", unsafe_allow_html=True)

    if not SELENIUM_AVAILABLE:
        st.error("Selenium is not installed - this tool cannot drive a browser without it.")

    bc1, _ = st.columns([2, 8])
    with bc1:
        if st.button("Next: Open Login Page →", use_container_width=True, disabled=not SELENIUM_AVAILABLE):
            terms = InputProcessor.from_manual(terms_raw)
            if not url_val.strip():
                st.error("Please enter the portal URL.")
            elif not terms:
                st.error("Please enter at least one search term.")
            else:
                S.portal_url = url_val.strip()
                S.search_terms = terms
                S.download_dir = dl_val.strip() or S.download_dir
                Path(S.download_dir).mkdir(parents=True, exist_ok=True)
                _launch_and_advance()


def _launch_and_advance() -> None:
    S = st.session_state
    log: LogStore = S.log_store
    with st.spinner("Launching Microsoft Edge and opening the portal..."):
        try:
            bm = BrowserManager(log, Path(S.download_dir))
            adapter = bm.launch()
            adapter.navigate(S.portal_url)
            S.browser_manager = bm
            S.step = 2
            st.rerun()
        except Exception as exc:
            st.error(f"Could not start the browser: {exc}")
            log.error(f"Browser launch failed: {exc}")


def render_step_2() -> None:
    S = st.session_state
    st.markdown("""
    <div class="login-box">
      <h3>🔐 Log into the portal first</h3>
      <p>The Edge window that just opened is already pointed at your portal. Log in with your
      normal credentials in that window, then come back here and click <b>I've logged in</b>.
      Nothing about your login is automated, read, or stored by this tool.</p>
    </div>
    """, unsafe_allow_html=True)

    bm: BrowserManager = S.browser_manager
    st.caption(f"Automation engine: **{bm.adapter.engine_name}**")

    c1, c2 = st.columns([2, 8])
    with c1:
        if st.button("✅ I've Logged In →", use_container_width=True):
            bm.adapter.minimize_to_corner()
            S.log_store.info("Automation window minimized to a corner - Streamlit is now the control center. "
                              "The Edge window keeps working in the background.")
            S.step = 3
            st.rerun()
    if st.button("← Back to Setup"):
        bm.close()
        S.browser_manager = None
        S.step = 1
        st.rerun()


def render_folder_gate() -> bool:
    """
    Ask Yes/No about folder-scoping before search starts.
    S.folder_decision states: None (not asked) -> "pending" (Yes clicked,
    choosing a folder) -> "resolved" (folder opened, or No was chosen).
    Returns True only once "resolved", i.e. safe to proceed to search.
    """
    S = st.session_state
    bm: BrowserManager = S.browser_manager
    log: LogStore = S.log_store

    if S.folder_decision == "resolved":
        return True

    st.markdown('<div class="card"><div class="card-title"><div class="dot"></div>Folder-Scoped Search?</div>', unsafe_allow_html=True)
    st.write("Do you want to search inside a specific folder?")
    col_yes, col_no = st.columns(2)
    with col_yes:
        if st.button("YES", use_container_width=True, disabled=S.folder_decision == "pending"):
            retry = RetryManager(log)
            ocr = OCRHelper(log)
            locator_engine = LocatorEngine(bm.adapter, log, retry, ocr)
            all_labels = locator_engine.discover_navigation()  # BeautifulSoup over the live authenticated page
            folder_like = [l for l in all_labels
                           if any(kw in l.lower() for kw in LocatorHints.FOLDER_KEYWORDS) or len(l.split()) <= 6]
            S.available_folders = folder_like or all_labels
            S.folder_decision = "pending"
            st.rerun()
    with col_no:
        if st.button("NO", use_container_width=True):
            S.folder_label = ""
            S.folder_decision = "resolved"
            st.rerun()

    if S.folder_decision == "pending":
        if S.available_folders:
            chosen = st.selectbox("Select a folder", S.available_folders)
            if st.button("Open Folder ➜", type="primary"):
                angular_engine = AngularEngine(bm.adapter, log)
                with st.spinner(f"Opening '{chosen}'..."):
                    ok = FolderNavigator(bm.adapter, angular_engine, log).open_folder(chosen)
                if ok:
                    S.folder_label = chosen
                    S.folder_decision = "resolved"
                    st.rerun()
                else:
                    st.error("Could not open that folder automatically. You can open it manually in Edge, then continue.")
                    if st.button("Continue anyway ➜"):
                        S.folder_label = chosen
                        S.folder_decision = "resolved"
                        st.rerun()
        else:
            st.warning("No folders were confidently detected.")
            if st.button("Continue without a folder ➜"):
                S.folder_label = ""
                S.folder_decision = "resolved"
                st.rerun()

    st.markdown("</div>", unsafe_allow_html=True)
    return False


def render_step_3() -> None:
    S = st.session_state
    bm: BrowserManager = S.browser_manager
    log: LogStore = S.log_store

    if not render_folder_gate():
        return

    st.markdown('<div class="card"><div class="card-title"><div class="dot"></div>Searching the Portal</div>', unsafe_allow_html=True)
    st.caption(f"Terms: {', '.join(S.search_terms)}" + (f" · Folder: {S.folder_label}" if S.folder_label else ""))
    st.markdown("</div>", unsafe_allow_html=True)

    if S.search_shared is None:
        shared = SharedSearchState()
        S.search_shared = shared
        thread = threading.Thread(
            target=search_worker,
            args=(bm.adapter, log, S.search_terms, S.folder_label or None, shared),
            daemon=True,
        )
        thread.start()
        st.rerun()

    shared: SharedSearchState = S.search_shared
    st.markdown(log.render_html(), unsafe_allow_html=True)

    if shared.done:
        with shared.lock:
            S.results = shared.results
        if S.results:
            st.success(f"✅ Found **{len(S.results)}** result(s). Review and select next.")
            if st.button("Next: Select Files →"):
                S.step = 4
                st.rerun()
        else:
            st.warning("No documents found. Check login status, the portal URL, folder name, or search terms.")
            c1, c2 = st.columns(2)
            with c1:
                if st.button("🔄 Try Again"):
                    S.search_shared = None
                    st.rerun()
            with c2:
                if st.button("← Back"):
                    S.step = 2
                    S.search_shared = None
                    S.folder_decision = None
                    st.rerun()
    else:
        time.sleep(1.0)
        st.rerun()


def render_browser_window_toggle() -> None:
    """Small convenience control to bring the minimized Edge window back to full size for a manual look."""
    S = st.session_state
    bm: Optional[BrowserManager] = S.get("browser_manager")
    if not bm or not bm.adapter:
        return
    c1, _ = st.columns([2, 8])
    with c1:
        if st.button("🔎 Show full-size browser window", key="restore_browser_btn"):
            bm.adapter.restore_size()
            st.toast("Browser window restored to full size.")


def render_step_4() -> None:
    S = st.session_state
    results: list[SearchResultItem] = S.results
    total = len(results)
    terms = {r.source_term for r in results}
    render_browser_window_toggle()

    st.markdown(f"""
    <div class="stats">
      <div class="stat"><div class="stat-v">{total}</div><div class="stat-l">Documents found</div></div>
      <div class="stat"><div class="stat-v">{len(terms)}</div><div class="stat-l">Search terms</div></div>
      <div class="stat"><div class="stat-v">{sum(1 for r in results if r.selected)}</div><div class="stat-l">Selected</div></div>
      <div class="stat"><div class="stat-v">📁</div><div class="stat-l">Ready to download</div></div>
    </div>
    """, unsafe_allow_html=True)

    st.markdown('<div class="card"><div class="card-title"><div class="dot"></div>Select Documents to Download</div>', unsafe_allow_html=True)

    fc1, fc2, fc3, fc4 = st.columns([2, 2, 1, 1])
    with fc1:
        filter_text = st.text_input("🔍 Filter by text", placeholder="type to filter…")
    with fc2:
        term_opts = ["All terms"] + sorted(terms)
        filter_term = st.selectbox("Filter by search term", term_opts)
    with fc3:
        st.markdown("<br>", unsafe_allow_html=True)
        if st.button("☑ Select all"):
            for r in results:
                r.selected = True
            st.rerun()
    with fc4:
        st.markdown("<br>", unsafe_allow_html=True)
        if st.button("☐ Deselect all"):
            for r in results:
                r.selected = False
            st.rerun()

    st.markdown("""
    <div class="result-hdr"><div>✓</div><div>Result</div><div>Search Term</div><div>Type</div></div>
    """, unsafe_allow_html=True)

    visible = [r for r in results
               if (not filter_text or filter_text.lower() in r.label.lower())
               and (filter_term == "All terms" or r.source_term == filter_term)]

    for i, item in enumerate(visible):
        checked = st.checkbox(item.label, value=item.selected, key=f"chk_{i}_{id(item)}",
                               label_visibility="collapsed")
        item.selected = checked
        ext = ext_of(item.label) or "—"
        st.markdown(f"""
        <div class="result-row" style="margin-top:-14px">
          <div></div>
          <div><div class="doc-title">{item.label}</div></div>
          <div><span class="lib-badge">{item.source_term}</span></div>
          <div style="color:#64748b;font-size:.8rem">{ext}</div>
        </div>
        """, unsafe_allow_html=True)

    selected_count = sum(1 for r in results if r.selected)
    st.markdown(f'<p style="color:#1a4fad;font-weight:600;margin-top:10px">{selected_count} of {total} selected</p>',
                unsafe_allow_html=True)
    st.markdown("</div>", unsafe_allow_html=True)

    st.info(
        "If a search term returned more than one result and you select more than one of them, "
        "the automation clicks the first visible download control after re-opening that term's "
        "results - it can't reliably tell rows apart without portal-specific hints. For guaranteed "
        "precision, prefer search terms specific enough to return exactly one result.",
        icon="ℹ️",
    )

    bc1, bc2, _ = st.columns([2, 2, 6])
    with bc1:
        if st.button("⬇️ Download Selected", disabled=selected_count == 0, use_container_width=True):
            st.session_state.step = 5
            st.rerun()
    with bc2:
        if st.button("← Back to Search", use_container_width=True):
            st.session_state.step = 3
            st.session_state.search_shared = None
            st.session_state.results = []
            st.rerun()


def render_step_5() -> None:
    S = st.session_state
    bm: BrowserManager = S.browser_manager
    log: LogStore = S.log_store
    selected = [r for r in S.results if r.selected]
    render_browser_window_toggle()

    st.markdown(
        f'<div class="card"><div class="card-title"><div class="dot"></div>'
        f'Downloading {len(selected)} Document(s) → {S.download_dir}</div></div>',
        unsafe_allow_html=True,
    )

    if S.download_shared is None:
        shared = SharedDownloadState()
        S.download_shared = shared
        thread = threading.Thread(
            target=download_worker,
            args=(bm.adapter, log, selected, Path(S.download_dir), shared),
            daemon=True,
        )
        thread.start()
        st.rerun()

    shared: SharedDownloadState = S.download_shared
    with shared.lock:
        done_n, total_n = shared.completed, shared.total
    st.progress((done_n / total_n) if total_n else 0.0, text=f"{done_n}/{total_n} processed")
    st.markdown(log.render_html(), unsafe_allow_html=True)

    if not shared.done:
        time.sleep(1.0)
        st.rerun()
        return

    with shared.lock:
        saved_paths = list(shared.saved_paths)
    ok, failed = len(saved_paths), total_n - len(saved_paths)

    st.markdown(f"""
    <div class="stats">
      <div class="stat"><div class="stat-v">{total_n}</div><div class="stat-l">Requested</div></div>
      <div class="stat"><div class="stat-v" style="color:#059669">{ok}</div><div class="stat-l">Downloaded</div></div>
      <div class="stat"><div class="stat-v" style="color:#dc2626">{failed}</div><div class="stat-l">Failed</div></div>
      <div class="stat"><div class="stat-v" style="color:#1a4fad">📁</div><div class="stat-l">Saved to folder</div></div>
    </div>
    """, unsafe_allow_html=True)

    if saved_paths:
        st.markdown('<div class="card"><div class="card-title"><div class="dot"></div>Downloaded Files</div>', unsafe_allow_html=True)
        for fp in saved_paths:
            p = Path(fp)
            size_kb = p.stat().st_size // 1024 if p.exists() else 0
            st.markdown(f"""
            <div style="background:#f8faff;border:1px solid #dce6f5;border-radius:8px;
                padding:10px 16px;margin-bottom:6px;display:flex;align-items:center;gap:12px">
              <span style="flex:1;color:#1e293b;font-weight:500;font-size:.86rem">{p.name}</span>
              <span style="color:#94a3b8;font-size:.75rem">{size_kb} KB</span>
              <span style="color:#059669;font-weight:700;font-size:.8rem">✓</span>
            </div>
            """, unsafe_allow_html=True)
        st.markdown("</div>", unsafe_allow_html=True)

        valid = [f for f in saved_paths if Path(f).exists()]
        if valid:
            zip_buffer = io.BytesIO()
            with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
                for fp in valid:
                    zf.write(fp, Path(fp).name)
            zip_buffer.seek(0)
            st.download_button(
                f"⬇️ Download all {len(valid)} files as ZIP",
                data=zip_buffer,
                file_name=f"portal_docs_{datetime.now():%Y%m%d_%H%M%S}.zip",
                mime="application/zip",
            )

    st.markdown("---")
    c1, c2 = st.columns(2)
    with c1:
        if st.button("🔄 New Search"):
            S.step, S.results, S.search_shared, S.download_shared = 3, [], None, None
            st.rerun()
    with c2:
        if st.button("🏠 Start Over"):
            full_reset()


# =============================================================================
# SECTION 15: MAIN
# =============================================================================
def main() -> None:
    init_session_state()
    S = st.session_state
    render_header(S.brand_name, S.brand_subtitle)
    render_step_bar(S.step)

    try:
        {1: render_step_1, 2: render_step_2, 3: render_step_3,
         4: render_step_4, 5: render_step_5}[S.step]()
    except Exception as exc:
        st.error("An unexpected error occurred. Details have been written to the log file.")
        S.log_store.error(f"Unhandled UI exception: {exc}\n{traceback.format_exc()}")

    st.markdown('<div class="footer">Portal Document Hunter — internal automation tool.</div>',
                unsafe_allow_html=True)


if __name__ == "__main__":
    main()
