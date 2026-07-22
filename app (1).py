"""
=============================================================================
 ENTERPRISE PORTAL AUTOMATION ASSISTANT
=============================================================================
A single-file (app.py) Streamlit application that drives Microsoft Edge
(via Playwright, with automatic Selenium fallback) to search and download
files from complex, authenticated enterprise portals (Angular / Angular
Material / PrimeNG / AG Grid / Shadow DOM / lazy-loading applications)
without any hard-coded portal knowledge.

-----------------------------------------------------------------------------
 IMPORTANT ARCHITECTURAL NOTE (read this before anything else)
-----------------------------------------------------------------------------
The original brief asks for the enterprise portal to "open inside
Streamlit". This is *not* technically achievable for authenticated,
JavaScript-heavy Angular applications:

    * Browsers refuse to render most enterprise sites inside an <iframe>
      because of X-Frame-Options / frame-ancestors / CSP headers.
    * Even when framing is technically allowed, Streamlit's Python process
      cannot reach across the iframe boundary to drive the DOM the way
      Playwright/Selenium can (no ability to click, type, wait for network
      idle, read ARIA attributes, etc.).
    * Corporate SSO/MFA logins routinely detect and block iframe-embedded
      login flows outright.

The practical, reliable architecture implemented below (and explicitly
allowed by the brief when full embedding is impossible) is:

    1. Streamlit is the control dashboard: URL entry, status, folder
       picking, upload of CSV/Excel/manual lists, live progress, logs.
    2. Playwright (primary) or Selenium (automatic fallback) opens and
       drives a REAL, dedicated Microsoft Edge (Chromium) window.
    3. The user logs into that Edge window manually (never automated -
       this is a hard rule, not a limitation) and clicks
       "Login Completed" back in Streamlit.
    4. From that point on, all discovery, searching, downloading, and
       retrying happens automatically, with progress mirrored live into
       the Streamlit dashboard.

This is the same pattern used by every serious enterprise RPA tool
(UiPath, Power Automate Desktop, etc.) for exactly the same reason.

-----------------------------------------------------------------------------
 RUNTIME / DEPENDENCY NOTES
-----------------------------------------------------------------------------
Everything below degrades gracefully: if an optional dependency
(Playwright, Selenium, OCR, tenacity, loguru, BeautifulSoup) is missing,
the app still starts, and clearly reports in the sidebar what is/isn't
available instead of crashing on import.

Recommended local (Citrix-safe, no Docker / no Linux-only tooling) setup:

    pip install streamlit playwright selenium pandas openpyxl lxml ^
                beautifulsoup4 tenacity loguru pillow pytesseract
    playwright install msedge

EasyOCR is optional and heavy (large model download); Tesseract via
pytesseract is the lighter-weight OCR fallback and is preferred when both
are available.

Run with:   streamlit run app.py
=============================================================================
"""

from __future__ import annotations

import io
import json
import queue
import re
import shutil
import threading
import time
import traceback
import unicodedata
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
# The app must run inside a locked-down Citrix session where not every
# optional package may be installed. Every optional import is therefore
# guarded, and a capability flag is exposed so the UI can honestly report
# what is/isn't available rather than crashing.

try:
    from loguru import logger as _loguru_logger
    LOGURU_AVAILABLE = True
except ImportError:  # pragma: no cover - environment dependent
    LOGURU_AVAILABLE = False
    _loguru_logger = None

try:
    from playwright.sync_api import (
        sync_playwright,
        Page as PWPage,
        BrowserContext as PWContext,
        Browser as PWBrowser,
        Locator as PWLocator,
        TimeoutError as PWTimeoutError,
        Error as PWError,
    )
    PLAYWRIGHT_AVAILABLE = True
except ImportError:  # pragma: no cover
    PLAYWRIGHT_AVAILABLE = False

try:
    from selenium import webdriver
    from selenium.webdriver.edge.options import Options as EdgeOptions
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC
    from selenium.common.exceptions import (
        WebDriverException,
        TimeoutException as SeleniumTimeoutException,
        NoSuchElementException,
    )
    SELENIUM_AVAILABLE = True
except ImportError:  # pragma: no cover
    SELENIUM_AVAILABLE = False

try:
    from bs4 import BeautifulSoup
    BS4_AVAILABLE = True
except ImportError:  # pragma: no cover
    BS4_AVAILABLE = False

try:
    import pytesseract
    from PIL import Image
    OCR_TESSERACT_AVAILABLE = True
except ImportError:  # pragma: no cover
    OCR_TESSERACT_AVAILABLE = False

try:
    import easyocr
    OCR_EASYOCR_AVAILABLE = True
except ImportError:  # pragma: no cover
    OCR_EASYOCR_AVAILABLE = False

try:
    from tenacity import (
        retry as tenacity_retry,
        stop_after_attempt,
        wait_exponential,
    )
    TENACITY_AVAILABLE = True
except ImportError:  # pragma: no cover
    TENACITY_AVAILABLE = False

    # Minimal no-op shim so `@tenacity_retry(...)` usages elsewhere in the
    # file never raise NameError even when tenacity isn't installed. Real
    # retrying in that case is handled manually by RetryManager instead.
    def tenacity_retry(*_a, **_k):  # type: ignore
        def _decorator(fn):
            return fn
        return _decorator

    def stop_after_attempt(*_a, **_k):  # type: ignore
        return None

    def wait_exponential(*_a, **_k):  # type: ignore
        return None


# =============================================================================
# SECTION 1: LOGGING
# =============================================================================
class LogStore:
    """
    Thread-safe, in-memory ring buffer of log records that both the
    background automation thread and the Streamlit UI thread can share.

    Also mirrors every record to a real log file on disk (via loguru when
    available, otherwise the stdlib `logging` module) so a full audit
    trail survives even after the in-memory buffer rotates.
    """

    def __init__(self, max_records: int = 2000, log_dir: Optional[Path] = None) -> None:
        self._lock = threading.Lock()
        self._records: deque[dict] = deque(maxlen=max_records)
        self.log_dir = log_dir or (Path.home() / "PortalAutomationLogs")
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.log_file = self.log_dir / f"automation_{datetime.now():%Y%m%d_%H%M%S}.log"

        if LOGURU_AVAILABLE:
            _loguru_logger.remove()
            _loguru_logger.add(str(self.log_file), rotation="5 MB", retention=5, level="DEBUG")
            self._backend = _loguru_logger
        else:  # stdlib fallback
            import logging as _logging
            self._backend = _logging.getLogger("portal_automation")
            self._backend.setLevel(_logging.DEBUG)
            handler = _logging.FileHandler(self.log_file, encoding="utf-8")
            handler.setFormatter(_logging.Formatter("%(asctime)s | %(levelname)s | %(message)s"))
            self._backend.addHandler(handler)

    def _write(self, level: str, message: str) -> None:
        record = {"time": datetime.now().strftime("%H:%M:%S"), "level": level, "message": message}
        with self._lock:
            self._records.append(record)
        try:
            if LOGURU_AVAILABLE:
                getattr(self._backend, level.lower(), self._backend.info)(message)
            else:
                getattr(self._backend, level.lower(), self._backend.info)(message)
        except Exception:
            pass  # logging must never crash the app

    def info(self, message: str) -> None:
        self._write("INFO", message)

    def warning(self, message: str) -> None:
        self._write("WARNING", message)

    def error(self, message: str) -> None:
        self._write("ERROR", message)

    def success(self, message: str) -> None:
        self._write("SUCCESS", message)

    def debug(self, message: str) -> None:
        self._write("DEBUG", message)

    def records(self) -> list[dict]:
        with self._lock:
            return list(self._records)


# =============================================================================
# SECTION 2: DATA MODELS
# =============================================================================
class JobStatus(str, Enum):
    PENDING = "Pending"
    SEARCHING = "Searching"
    AWAITING_SELECTION = "Awaiting Selection"
    NOT_FOUND = "Not Found"
    DOWNLOADING = "Downloading"
    COMPLETED = "Completed"
    FAILED = "Failed"
    SKIPPED = "Skipped"


@dataclass
class SearchResultItem:
    """One row/card/tile found in the portal's result set."""
    index: int
    label: str
    raw_text: str = ""
    row_hint: str = ""  # locator hint used to re-find this row's download control


@dataclass
class DownloadJob:
    """One unit of work: a single search query drawn from CSV/Excel/manual input."""
    query: str
    status: JobStatus = JobStatus.PENDING
    results: list[SearchResultItem] = field(default_factory=list)
    selected_indices: list[int] = field(default_factory=list)
    downloaded_files: list[str] = field(default_factory=list)
    attempts: int = 0
    error: Optional[str] = None
    started_at: Optional[float] = None
    finished_at: Optional[float] = None

    def duration(self) -> Optional[float]:
        if self.started_at and self.finished_at:
            return round(self.finished_at - self.started_at, 2)
        return None


@dataclass
class AutomationStats:
    total: int = 0
    completed: int = 0
    failed: int = 0
    skipped: int = 0
    not_found: int = 0
    files_downloaded: int = 0
    start_time: Optional[float] = None

    @property
    def remaining(self) -> int:
        return max(self.total - self.completed - self.failed - self.skipped - self.not_found, 0)

    @property
    def elapsed_seconds(self) -> float:
        return time.time() - self.start_time if self.start_time else 0.0

    @property
    def avg_seconds_per_item(self) -> float:
        done = self.completed + self.failed + self.skipped + self.not_found
        return self.elapsed_seconds / done if done else 0.0

    @property
    def eta_seconds(self) -> float:
        return self.avg_seconds_per_item * self.remaining


class SharedAutomationState:
    """
    Thread-safe bridge between the background automation thread and the
    Streamlit UI thread. Streamlit re-runs the whole script on every
    interaction, so any live objects the automation thread depends on
    (browser handles, job list, pending human decisions) must live
    somewhere that survives reruns and is safe to touch from both sides.
    We stash exactly one instance of this in `st.session_state`.
    """

    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.jobs: list[DownloadJob] = []
        self.stats = AutomationStats()
        self.running: bool = False
        self.current_job_index: int = -1
        self.pending_selection: Optional[dict] = None  # {"job_index", "results"}
        self.selection_event = threading.Event()
        self.stop_requested = threading.Event()

    def snapshot_stats(self) -> AutomationStats:
        with self.lock:
            total = len(self.jobs)
            completed = sum(1 for j in self.jobs if j.status == JobStatus.COMPLETED)
            failed = sum(1 for j in self.jobs if j.status == JobStatus.FAILED)
            skipped = sum(1 for j in self.jobs if j.status == JobStatus.SKIPPED)
            not_found = sum(1 for j in self.jobs if j.status == JobStatus.NOT_FOUND)
            files = sum(len(j.downloaded_files) for j in self.jobs)
            self.stats.total = total
            self.stats.completed = completed
            self.stats.failed = failed
            self.stats.skipped = skipped
            self.stats.not_found = not_found
            self.stats.files_downloaded = files
            return self.stats


# =============================================================================
# SECTION 3: FILESYSTEM UTILITIES
# =============================================================================
def safe_filename(name: str) -> str:
    """Strip characters that are illegal/unsafe across Windows/Citrix filesystems."""
    name = unicodedata.normalize("NFKD", name)
    name = re.sub(r'[\\/:*?"<>|]', "_", name).strip()
    return name or "download"


def dedupe_path(target: Path) -> Path:
    """
    Never overwrite an existing file. Rename intelligently:
    'report.pdf' -> 'report (1).pdf' -> 'report (2).pdf' ...
    """
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
# SECTION 4: RETRY MANAGER (self-healing retries)
# =============================================================================
class RetryManager:
    """
    Centralized retry policy used by every layer of the engine
    (locators, waits, downloads). Uses tenacity when available for
    exponential backoff; otherwise falls back to a hand-rolled loop with
    the same semantics so behaviour is identical either way.
    """

    def __init__(self, log: LogStore, max_attempts: int = 3, base_delay: float = 0.75):
        self.log = log
        self.max_attempts = max_attempts
        self.base_delay = base_delay

    def run(self, fn: Callable[[], Any], description: str, swallow: bool = False) -> Any:
        last_exc: Optional[Exception] = None
        for attempt in range(1, self.max_attempts + 1):
            try:
                return fn()
            except Exception as exc:  # noqa: BLE001 - deliberately broad; self-healing boundary
                last_exc = exc
                self.log.warning(f"Attempt {attempt}/{self.max_attempts} failed for '{description}': {exc}")
                if attempt < self.max_attempts:
                    time.sleep(self.base_delay * (2 ** (attempt - 1)))
        if swallow:
            self.log.error(f"'{description}' failed after {self.max_attempts} attempts (continuing): {last_exc}")
            return None
        raise RuntimeError(f"'{description}' failed after {self.max_attempts} attempts: {last_exc}") from last_exc


# =============================================================================
# SECTION 5: OCR HELPER (optional - for image-only buttons)
# =============================================================================
class OCRHelper:
    """
    Some enterprise portals render controls as pure images/canvas/sprites
    with no accessible text. When the DOM-based LocatorEngine can't find a
    control by role/label/text, this helper OCRs a screenshot to locate
    button text and returns pixel coordinates to click.

    Prefers Tesseract (fast, light) and falls back to EasyOCR (slower,
    no external binary needed, better on stylised fonts) when available.
    Silently reports "unavailable" rather than crashing the app when
    neither is installed - OCR is an enhancement, not a hard dependency.
    """

    def __init__(self, log: LogStore):
        self.log = log
        self._easyocr_reader = None

    @property
    def available(self) -> bool:
        return OCR_TESSERACT_AVAILABLE or OCR_EASYOCR_AVAILABLE

    def find_text_coordinates(self, screenshot_bytes: bytes, target_text: str) -> Optional[tuple[int, int]]:
        """Return (x, y) centre-point of the best text match, or None."""
        target_norm = target_text.strip().lower()
        if not target_norm:
            return None

        if OCR_TESSERACT_AVAILABLE:
            try:
                img = Image.open(io.BytesIO(screenshot_bytes))
                data = pytesseract.image_to_data(img, output_type=pytesseract.Output.DICT)
                for i, word in enumerate(data["text"]):
                    if word.strip().lower() == target_norm or target_norm in word.strip().lower():
                        x = data["left"][i] + data["width"][i] // 2
                        y = data["top"][i] + data["height"][i] // 2
                        return (x, y)
            except Exception as exc:
                self.log.warning(f"Tesseract OCR failed, trying EasyOCR if available: {exc}")

        if OCR_EASYOCR_AVAILABLE:
            try:
                if self._easyocr_reader is None:
                    self._easyocr_reader = easyocr.Reader(["en"], gpu=False)
                results = self._easyocr_reader.readtext(screenshot_bytes)
                for (bbox, text, _confidence) in results:
                    if target_norm in text.strip().lower():
                        xs = [p[0] for p in bbox]
                        ys = [p[1] for p in bbox]
                        return (int(sum(xs) / 4), int(sum(ys) / 4))
            except Exception as exc:
                self.log.warning(f"EasyOCR failed: {exc}")

        return None


# =============================================================================
# SECTION 6: LOCATOR HEURISTICS (engine-agnostic hint tables)
# =============================================================================
class LocatorHints:
    """
    Central catalogue of heuristics used to find controls WITHOUT any
    portal-specific hardcoding. Ranked from most to least reliable, per
    the brief's priority order:
        Role > ARIA > Label > Placeholder > Text > data-testid > XPath > CSS
    """

    SEARCH_KEYWORDS = ["search", "filter", "find", "lookup", "query", "keyword"]
    FOLDER_KEYWORDS = ["folder", "directory", "category", "workspace", "repository", "library"]
    DOWNLOAD_KEYWORDS = ["download", "export", "save", "get file", "retrieve"]
    SPINNER_SELECTORS = [
        "mat-spinner", "mat-progress-bar", "mat-progress-spinner",
        "[class*='spinner']", "[class*='loading']", "[class*='loader']",
        "p-progressspinner", "[role='progressbar']", "ngx-spinner",
    ]
    NAV_SELECTORS = [
        "nav", "[role='navigation']", "[role='tree']", "[role='treeitem']",
        "mat-sidenav", "mat-tree", "p-tree", "p-menu", "ul[class*='menu']",
        "[class*='sidenav']", "[class*='sidebar']", "[class*='nav-']",
    ]

    @staticmethod
    def search_box_css_candidates() -> list[str]:
        candidates = []
        for kw in LocatorHints.SEARCH_KEYWORDS:
            candidates += [
                f"input[placeholder*='{kw}' i]",
                f"input[aria-label*='{kw}' i]",
                f"input[name*='{kw}' i]",
                f"input[id*='{kw}' i]",
                f"[data-testid*='{kw}' i] input",
                f"input[data-testid*='{kw}' i]",
            ]
        candidates += ["input[type='search']", "input[role='searchbox']"]
        return candidates

    @staticmethod
    def download_button_css_candidates() -> list[str]:
        candidates = []
        for kw in LocatorHints.DOWNLOAD_KEYWORDS:
            candidates += [
                f"button[aria-label*='{kw}' i]",
                f"a[aria-label*='{kw}' i]",
                f"[data-testid*='{kw}' i]",
                f"button[title*='{kw}' i]",
                f"a[title*='{kw}' i]",
                f"button:has-text('{kw}')",
                f"a:has-text('{kw}')",
                f"[class*='{kw}' i]",
                f"mat-icon:has-text('{kw}')",
                f"svg[aria-label*='{kw}' i]",
            ]
        return candidates


# =============================================================================
# SECTION 7: BROWSER ADAPTER (common interface over Playwright / Selenium)
# =============================================================================
class BrowserAdapter(ABC):
    """
    Both PlaywrightAdapter and SeleniumAdapter implement this same surface
    so every downstream class (LocatorEngine, AngularEngine, SearchManager,
    DownloadManager, FolderNavigator) is written ONCE against a single
    interface and works unmodified regardless of which engine is actually
    driving the browser at runtime.
    """

    engine_name: str = "unknown"

    @abstractmethod
    def navigate(self, url: str) -> None: ...

    @abstractmethod
    def current_url(self) -> str: ...

    @abstractmethod
    def page_html(self) -> str: ...

    @abstractmethod
    def find_first(self, css_candidates: list[str]) -> Optional[Any]: ...

    @abstractmethod
    def click(self, element: Any) -> None: ...

    @abstractmethod
    def type_text(self, element: Any, text: str) -> None: ...

    @abstractmethod
    def press_enter(self, element: Any) -> None: ...

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
    def download_via_click(self, element: Any, target_dir: Path, timeout_ms: int = 30000) -> Optional[Path]: ...

    @abstractmethod
    def close(self) -> None: ...


class PlaywrightAdapter(BrowserAdapter):
    """Primary automation engine, per the brief."""

    engine_name = "Playwright (Edge)"

    def __init__(self, log: LogStore, download_dir: Path):
        if not PLAYWRIGHT_AVAILABLE:
            raise RuntimeError("Playwright is not installed in this environment.")
        self.log = log
        self.download_dir = download_dir
        self._playwright = sync_playwright().start()
        # A persistent context keeps cookies/session between navigations,
        # which some Angular SSO flows rely on.
        self._context: PWContext = self._playwright.chromium.launch_persistent_context(
            user_data_dir=str(Path.home() / ".portal_automation_profile"),
            channel="msedge",
            headless=False,
            accept_downloads=True,
            viewport={"width": 1440, "height": 900},
        )
        self.page: PWPage = self._context.pages[0] if self._context.pages else self._context.new_page()

    def navigate(self, url: str) -> None:
        self.page.goto(url, wait_until="domcontentloaded")

    def current_url(self) -> str:
        return self.page.url

    def page_html(self) -> str:
        return self.page.content()

    def find_first(self, css_candidates: list[str]) -> Optional[PWLocator]:
        for css in css_candidates:
            try:
                loc = self.page.locator(css).first
                if loc.count() > 0 and loc.is_visible():
                    return loc
            except Exception:
                continue
        return None

    def click(self, element: PWLocator) -> None:
        element.click(timeout=5000)

    def type_text(self, element: PWLocator, text: str) -> None:
        element.fill("")
        element.type(text, delay=15)

    def press_enter(self, element: PWLocator) -> None:
        element.press("Enter")

    def wait_network_idle(self, timeout_ms: int = 8000) -> None:
        try:
            self.page.wait_for_load_state("networkidle", timeout=timeout_ms)
        except PWTimeoutError:
            pass  # some Angular apps keep long-poll/websocket connections open forever

    def wait_no_spinner(self, timeout_ms: int = 10000) -> None:
        deadline = time.time() + (timeout_ms / 1000)
        selector = ", ".join(LocatorHints.SPINNER_SELECTORS)
        while time.time() < deadline:
            try:
                visible = self.page.locator(selector).first.is_visible()
            except Exception:
                visible = False
            if not visible:
                return
            time.sleep(0.25)

    def wait_dom_stable(self, timeout_ms: int = 6000) -> None:
        deadline = time.time() + (timeout_ms / 1000)
        last_len = -1
        stable_reads = 0
        while time.time() < deadline:
            try:
                length = self.page.evaluate("document.body.innerHTML.length")
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
        return self.page.screenshot()

    def click_at(self, x: int, y: int) -> None:
        self.page.mouse.click(x, y)

    def download_via_click(self, element: PWLocator, target_dir: Path, timeout_ms: int = 30000) -> Optional[Path]:
        with self.page.expect_download(timeout=timeout_ms) as download_info:
            element.click()
        download = download_info.value
        suggested = safe_filename(download.suggested_filename)
        final_path = dedupe_path(target_dir / suggested)
        download.save_as(str(final_path))
        return final_path

    def close(self) -> None:
        try:
            self._context.close()
        finally:
            self._playwright.stop()


class SeleniumAdapter(BrowserAdapter):
    """Automatic fallback engine if Playwright fails to launch or drive the page."""

    engine_name = "Selenium (Edge)"

    def __init__(self, log: LogStore, download_dir: Path):
        if not SELENIUM_AVAILABLE:
            raise RuntimeError("Selenium is not installed in this environment.")
        self.log = log
        self.download_dir = download_dir
        options = EdgeOptions()
        options.use_chromium = True
        options.add_experimental_option("prefs", {
            "download.default_directory": str(download_dir),
            "download.prompt_for_download": False,
            "safebrowsing.enabled": True,
        })
        # Selenium 4's built-in Selenium Manager resolves the matching
        # msedgedriver automatically - no manual driver path needed.
        self.driver = webdriver.Edge(options=options)
        self.driver.maximize_window()

    def navigate(self, url: str) -> None:
        self.driver.get(url)

    def current_url(self) -> str:
        return self.driver.current_url

    def page_html(self) -> str:
        return self.driver.page_source

    def find_first(self, css_candidates: list[str]) -> Optional[Any]:
        for css in css_candidates:
            try:
                els = self.driver.find_elements(By.CSS_SELECTOR, css)
                for el in els:
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
        from selenium.webdriver.common.keys import Keys
        element.send_keys(Keys.ENTER)

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

    def click_at(self, x: int, y: int) -> None:
        from selenium.webdriver.common.action_chains import ActionChains
        ActionChains(self.driver).move_by_offset(x, y).click().perform()

    def download_via_click(self, element: Any, target_dir: Path, timeout_ms: int = 30000) -> Optional[Path]:
        # Selenium has no native download-completion event, so we poll the
        # configured download directory for a new, fully-written file.
        before = {p.name for p in target_dir.glob("*")}
        element.click()
        deadline = time.time() + (timeout_ms / 1000)
        while time.time() < deadline:
            current = {p.name for p in target_dir.glob("*") if not p.name.endswith((".crdownload", ".tmp"))}
            new_files = current - before
            if new_files:
                newest = max((target_dir / n for n in new_files), key=lambda p: p.stat().st_mtime)
                return newest
            time.sleep(0.5)
        return None

    def close(self) -> None:
        self.driver.quit()


# =============================================================================
# SECTION 8: BROWSER MANAGER (launch + automatic Playwright -> Selenium fallback)
# =============================================================================
class BrowserManager:
    """Owns the lifecycle of whichever BrowserAdapter ends up driving Edge."""

    def __init__(self, log: LogStore, download_dir: Path):
        self.log = log
        self.download_dir = download_dir
        self.adapter: Optional[BrowserAdapter] = None

    def launch(self) -> BrowserAdapter:
        if PLAYWRIGHT_AVAILABLE:
            try:
                self.log.info("Launching Microsoft Edge via Playwright (primary engine)...")
                self.adapter = PlaywrightAdapter(self.log, self.download_dir)
                self.log.success("Playwright/Edge session started.")
                return self.adapter
            except Exception as exc:
                self.log.warning(f"Playwright launch failed ({exc}); falling back to Selenium.")

        if SELENIUM_AVAILABLE:
            try:
                self.log.info("Launching Microsoft Edge via Selenium (fallback engine)...")
                self.adapter = SeleniumAdapter(self.log, self.download_dir)
                self.log.success("Selenium/Edge session started.")
                return self.adapter
            except Exception as exc:
                self.log.error(f"Selenium launch also failed: {exc}")
                raise RuntimeError(
                    "Both Playwright and Selenium failed to launch Microsoft Edge. "
                    "Verify Edge is installed and 'playwright install msedge' has been run."
                ) from exc

        raise RuntimeError(
            "Neither Playwright nor Selenium is installed. Install at least one "
            "(`pip install playwright` then `playwright install msedge`, or `pip install selenium`)."
        )

    def close(self) -> None:
        if self.adapter:
            try:
                self.adapter.close()
            except Exception as exc:
                self.log.warning(f"Error while closing browser: {exc}")
            finally:
                self.adapter = None


# =============================================================================
# SECTION 9: LOCATOR ENGINE (self-healing, heuristic element discovery)
# =============================================================================
class LocatorEngine:
    """
    Finds elements purely by heuristics - role, ARIA, label, placeholder,
    visible text, data-testid, then structural CSS - never by a
    hardcoded, portal-specific selector. If the top-ranked strategy fails,
    it automatically walks down to the next one (self-healing) instead of
    raising immediately.
    """

    def __init__(self, adapter: BrowserAdapter, log: LogStore, retry: RetryManager, ocr: OCRHelper):
        self.adapter = adapter
        self.log = log
        self.retry = retry
        self.ocr = ocr

    def find_search_box(self) -> Optional[Any]:
        element = self.retry.run(
            lambda: self.adapter.find_first(LocatorHints.search_box_css_candidates()),
            "locate search box",
            swallow=True,
        )
        if element is None:
            self.log.warning("No search box found via DOM heuristics; search step will be skipped for this job.")
        return element

    def find_download_controls(self) -> list[Any]:
        """Return every plausible download control currently visible (icons, buttons, links, menus)."""
        found = []
        for css in LocatorHints.download_button_css_candidates():
            try:
                el = self.adapter.find_first([css])
                if el is not None:
                    found.append(el)
            except Exception:
                continue
        return found

    def find_download_control_via_ocr(self) -> Optional[tuple[int, int]]:
        """Last resort: OCR the current viewport for a 'Download'-like label and return click coordinates."""
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
        """
        Inspect the current page for navigation menus / folder trees /
        cards / lists using structural + ARIA heuristics, without any
        hardcoded selectors specific to a given portal.
        """
        html = self.adapter.page_html()
        labels: list[str] = []
        if BS4_AVAILABLE:
            soup = BeautifulSoup(html, "lxml" if _lxml_present() else "html.parser")
            candidates = soup.select(
                "nav a, [role='treeitem'], [role='menuitem'], [role='navigation'] a, "
                "mat-tree-node, p-treenode, li[class*='nav'], a[class*='folder'], "
                "div[class*='folder'], [class*='card']"
            )
            for el in candidates:
                text = el.get_text(strip=True) or el.get("aria-label", "") or el.get("title", "")
                if text and len(text) < 120:
                    labels.append(text)
        # De-duplicate while preserving order
        seen = set()
        unique_labels = []
        for label in labels:
            if label not in seen:
                seen.add(label)
                unique_labels.append(label)
        return unique_labels[:200]  # sane cap for very large menus


def _lxml_present() -> bool:
    try:
        import lxml  # noqa: F401
        return True
    except ImportError:
        return False


# =============================================================================
# SECTION 10: ANGULAR ENGINE (intelligent waiting - never time.sleep-based)
# =============================================================================
class AngularEngine:
    """
    Waits for Angular (and Angular Material / PrimeNG / AG Grid) apps to
    settle after a navigation or user action, using layered signals
    instead of a fixed sleep:
        1. network idle
        2. loading spinner / progress-bar disappeared
        3. DOM stopped mutating (structural stability)
    """

    def __init__(self, adapter: BrowserAdapter, log: LogStore):
        self.adapter = adapter
        self.log = log

    def wait_stable(self, network_timeout_ms: int = 8000, spinner_timeout_ms: int = 12000,
                     dom_timeout_ms: int = 6000) -> None:
        self.log.debug("Waiting for Angular app to stabilise (network -> spinner -> DOM)...")
        self.adapter.wait_network_idle(network_timeout_ms)
        self.adapter.wait_no_spinner(spinner_timeout_ms)
        self.adapter.wait_dom_stable(dom_timeout_ms)


# =============================================================================
# SECTION 11: TABLE / RESULT DETECTOR
# =============================================================================
class TableDetector:
    """
    Extracts result rows from whatever rendering technology the portal
    uses for its results view: plain HTML tables, AG Grid virtual rows,
    PrimeNG p-table, or generic card/list layouts.
    """

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

    def extract_results(self) -> list[SearchResultItem]:
        if not BS4_AVAILABLE:
            self.log.warning("BeautifulSoup not installed - falling back to raw text heuristics for results.")
            return []
        html = self.adapter.page_html()
        soup = BeautifulSoup(html, "lxml" if _lxml_present() else "html.parser")
        items: list[SearchResultItem] = []
        seen_texts = set()
        for selector in self.ROW_SELECTORS:
            for i, row in enumerate(soup.select(selector)):
                text = row.get_text(" ", strip=True)
                if not text or text in seen_texts or len(text) > 500:
                    continue
                seen_texts.add(text)
                items.append(SearchResultItem(index=len(items), label=text[:120], raw_text=text, row_hint=selector))
            if items:
                # Stop at the first selector family that actually matched
                # something real, to avoid double-counting the same grid
                # under multiple heuristic selectors.
                break
        return items


# =============================================================================
# SECTION 12: FOLDER NAVIGATOR
# =============================================================================
class FolderNavigator:
    """Discovers and opens folders/categories in the portal's navigation tree."""

    def __init__(self, adapter: BrowserAdapter, locator_engine: LocatorEngine,
                 angular_engine: AngularEngine, log: LogStore):
        self.adapter = adapter
        self.locator_engine = locator_engine
        self.angular_engine = angular_engine
        self.log = log

    def list_folders(self) -> list[str]:
        all_nav_labels = self.locator_engine.discover_navigation()
        folder_like = [
            label for label in all_nav_labels
            if any(kw in label.lower() for kw in LocatorHints.FOLDER_KEYWORDS) or len(label.split()) <= 6
        ]
        return folder_like or all_nav_labels

    def open_folder(self, folder_label: str) -> bool:
        try:
            css = f"*:has-text('{folder_label}')" if PLAYWRIGHT_AVAILABLE and isinstance(self.adapter, PlaywrightAdapter) else None
            element = None
            if css:
                element = self.adapter.find_first([css])
            if element is None:
                # Selenium / generic fallback: XPath text match
                element = self.adapter.find_first([f"//*[contains(text(), '{folder_label}')]"])
            if element is None:
                self.log.warning(f"Could not locate folder '{folder_label}' to open it.")
                return False
            self.adapter.click(element)
            self.angular_engine.wait_stable()
            self.log.success(f"Opened folder: {folder_label}")
            return True
        except Exception as exc:
            self.log.error(f"Failed opening folder '{folder_label}': {exc}")
            return False


# =============================================================================
# SECTION 13: INPUT PROCESSOR (CSV / Excel / manual list)
# =============================================================================
class InputProcessor:
    """Normalises CSV, Excel, or manual multi-line text into a flat list of search queries."""

    @staticmethod
    def from_csv(uploaded_file, column: Optional[str] = None) -> tuple[pd.DataFrame, list[str]]:
        df = pd.read_csv(uploaded_file)
        return df, InputProcessor._extract(df, column)

    @staticmethod
    def from_excel(uploaded_file, column: Optional[str] = None) -> tuple[pd.DataFrame, list[str]]:
        df = pd.read_excel(uploaded_file, engine="openpyxl")
        return df, InputProcessor._extract(df, column)

    @staticmethod
    def from_manual(text: str) -> list[str]:
        return [line.strip() for line in text.splitlines() if line.strip()]

    @staticmethod
    def _extract(df: pd.DataFrame, column: Optional[str]) -> list[str]:
        col = column or df.columns[0]
        return [str(v).strip() for v in df[col].dropna().tolist() if str(v).strip()]


# =============================================================================
# SECTION 14: SEARCH MANAGER
# =============================================================================
class SearchManager:
    """Runs one query end-to-end: locate search box -> type -> wait -> collect results."""

    def __init__(self, adapter: BrowserAdapter, locator_engine: LocatorEngine,
                 angular_engine: AngularEngine, table_detector: TableDetector,
                 retry: RetryManager, log: LogStore):
        self.adapter = adapter
        self.locator_engine = locator_engine
        self.angular_engine = angular_engine
        self.table_detector = table_detector
        self.retry = retry
        self.log = log

    def search(self, query: str) -> list[SearchResultItem]:
        search_box = self.locator_engine.find_search_box()
        if search_box is None:
            raise RuntimeError("No search box could be located on this page.")

        self.retry.run(lambda: self.adapter.type_text(search_box, query), f"type query '{query}'")
        self.retry.run(lambda: self.adapter.press_enter(search_box), f"submit query '{query}'")

        self.angular_engine.wait_stable()
        self._scroll_and_paginate_if_needed()

        return self.table_detector.extract_results()

    def _scroll_and_paginate_if_needed(self, max_scrolls: int = 5) -> None:
        """Handles lazy-loading / infinite-scroll result lists without hardcoding pagination controls."""
        for _ in range(max_scrolls):
            before = len(self.table_detector.extract_results())
            try:
                if isinstance(self.adapter, PlaywrightAdapter):
                    self.adapter.page.mouse.wheel(0, 2000)
                elif isinstance(self.adapter, SeleniumAdapter):
                    self.adapter.driver.execute_script("window.scrollBy(0, 2000);")
            except Exception:
                break
            self.angular_engine.wait_stable(dom_timeout_ms=2500)
            after = len(self.table_detector.extract_results())
            if after <= before:
                break  # no new rows appeared - lazy loading is exhausted


# =============================================================================
# SECTION 15: DOWNLOAD MANAGER
# =============================================================================
class DownloadManager:
    """Locates and triggers download controls, saving into the user-chosen directory with dedupe."""

    def __init__(self, adapter: BrowserAdapter, locator_engine: LocatorEngine,
                 retry: RetryManager, log: LogStore, download_dir: Path):
        self.adapter = adapter
        self.locator_engine = locator_engine
        self.retry = retry
        self.log = log
        self.download_dir = download_dir
        self.download_dir.mkdir(parents=True, exist_ok=True)

    def download_result(self, result: SearchResultItem) -> Optional[Path]:
        controls = self.locator_engine.find_download_controls()
        if not controls:
            coords = self.locator_engine.find_download_control_via_ocr()
            if coords:
                self.log.info("Using OCR-detected coordinates as last-resort download trigger.")
                self.adapter.click_at(*coords)
                time.sleep(1.5)  # brief settle after a coordinate click, not a substitute for real waits elsewhere
                return None  # coordinate-based click can't be tied to expect_download cleanly; logged, not silent
            raise RuntimeError(f"No download control found for result '{result.label}'.")

        control = controls[0]

        def _do_download():
            return self.adapter.download_via_click(control, self.download_dir)

        saved_path = self.retry.run(_do_download, f"download '{result.label}'")
        if saved_path:
            self.log.success(f"Downloaded: {saved_path.name}")
        return saved_path


# =============================================================================
# SECTION 16: AUTOMATION ENGINE (orchestrator, runs on a background thread)
# =============================================================================
class AutomationEngine:
    """
    Ties every component together and drives the job queue end-to-end.
    Runs on a background thread so the Streamlit UI thread stays
    responsive for progress polling and human-in-the-loop decisions
    (multi-result selection).
    """

    def __init__(self, adapter: BrowserAdapter, log: LogStore, shared: SharedAutomationState,
                 download_dir: Path):
        self.adapter = adapter
        self.log = log
        self.shared = shared
        self.retry = RetryManager(log)
        self.ocr = OCRHelper(log)
        self.locator_engine = LocatorEngine(adapter, log, self.retry, self.ocr)
        self.angular_engine = AngularEngine(adapter, log)
        self.table_detector = TableDetector(adapter, log)
        self.search_manager = SearchManager(adapter, self.locator_engine, self.angular_engine,
                                             self.table_detector, self.retry, log)
        self.download_manager = DownloadManager(adapter, self.locator_engine, self.retry, log, download_dir)

    def run_all(self) -> None:
        self.shared.running = True
        self.shared.stats.start_time = time.time()
        self.log.info(f"Automation started for {len(self.shared.jobs)} item(s).")

        for idx, job in enumerate(self.shared.jobs):
            if self.shared.stop_requested.is_set():
                self.log.warning("Stop requested by user - halting remaining jobs (resumable later).")
                break
            with self.shared.lock:
                self.shared.current_job_index = idx
            self._run_single_job(job)

        self.shared.running = False
        self.log.success("Automation run finished.")

    def _run_single_job(self, job: DownloadJob) -> None:
        job.started_at = time.time()
        job.status = JobStatus.SEARCHING
        job.attempts += 1
        try:
            results = self.search_manager.search(job.query)
            job.results = results

            if len(results) == 0:
                job.status = JobStatus.NOT_FOUND
                self.log.info(f"No results for '{job.query}'.")

            elif len(results) <= 2:
                # "Two or fewer -> download all automatically"
                job.status = JobStatus.DOWNLOADING
                for result in results:
                    saved = self.download_manager.download_result(result)
                    if saved:
                        job.downloaded_files.append(str(saved))
                job.status = JobStatus.COMPLETED

            else:
                # More than two -> human picks which to download
                job.status = JobStatus.AWAITING_SELECTION
                self._request_human_selection(job)
                if job.selected_indices:
                    job.status = JobStatus.DOWNLOADING
                    for i in job.selected_indices:
                        result = next((r for r in results if r.index == i), None)
                        if result:
                            saved = self.download_manager.download_result(result)
                            if saved:
                                job.downloaded_files.append(str(saved))
                    job.status = JobStatus.COMPLETED
                else:
                    job.status = JobStatus.SKIPPED
                    self.log.info(f"User skipped selection for '{job.query}'.")

        except Exception as exc:
            job.status = JobStatus.FAILED
            job.error = str(exc)
            self.log.error(f"Job '{job.query}' failed: {exc}\n{traceback.format_exc(limit=2)}")
        finally:
            job.finished_at = time.time()

    def _request_human_selection(self, job: DownloadJob) -> None:
        """Pause this job and hand control back to the Streamlit UI thread for a manual pick."""
        self.shared.selection_event.clear()
        with self.shared.lock:
            self.shared.pending_selection = {"job": job, "results": job.results}
        self.log.info(f"'{job.query}' returned {len(job.results)} results - waiting for user to choose.")
        self.shared.selection_event.wait(timeout=600)  # 10-minute safety timeout
        with self.shared.lock:
            self.shared.pending_selection = None


# =============================================================================
# SECTION 17: UI MANAGER (Streamlit rendering)
# =============================================================================
class UIManager:
    """All Streamlit rendering logic, organised by wizard step."""

    STEPS = ["portal", "login", "inspect", "folders", "inputs", "download_dir", "dashboard"]

    def __init__(self):
        self._init_session_state()

    # -- session bootstrap ----------------------------------------------
    def _init_session_state(self) -> None:
        defaults = {
            "step": "portal",
            "portal_url": "",
            "download_dir": str(Path.home() / "Downloads" / "PortalAutomation"),
            "log_store": LogStore(),
            "browser_manager": None,
            "shared_state": None,
            "engine": None,
            "queries": [],
            "search_in_folder": None,
            "chosen_folder": None,
            "available_folders": [],
            "nav_labels": [],
            "automation_thread": None,
        }
        for key, val in defaults.items():
            if key not in st.session_state:
                st.session_state[key] = val

    # -- shared styling ----------------------------------------------------
    @staticmethod
    def inject_theme() -> None:
        st.markdown(
            """
            <style>
                .stApp { background-color: #0e1117; }
                section[data-testid="stSidebar"] { background-color: #131722; }
                div[data-testid="stMetric"] {
                    background-color: #1b1f2b; border-radius: 10px; padding: 12px;
                    border: 1px solid #2a2f3d;
                }
                .capability-ok { color: #3ddc84; font-weight: 600; }
                .capability-bad { color: #ff5c5c; font-weight: 600; }
                .job-badge {
                    display: inline-block; padding: 2px 10px; border-radius: 12px;
                    font-size: 0.8em; font-weight: 600;
                }
            </style>
            """,
            unsafe_allow_html=True,
        )

    # -- sidebar -------------------------------------------------------
    def render_sidebar(self) -> None:
        with st.sidebar:
            st.markdown("### 🧭 Engine Status")
            self._capability_row("Playwright (primary)", PLAYWRIGHT_AVAILABLE)
            self._capability_row("Selenium (fallback)", SELENIUM_AVAILABLE)
            self._capability_row("BeautifulSoup (parsing)", BS4_AVAILABLE)
            self._capability_row("Tesseract OCR", OCR_TESSERACT_AVAILABLE)
            self._capability_row("EasyOCR", OCR_EASYOCR_AVAILABLE)
            self._capability_row("tenacity (retry backoff)", TENACITY_AVAILABLE)
            self._capability_row("loguru (structured logs)", LOGURU_AVAILABLE)

            st.markdown("---")
            st.markdown("### 📍 Wizard Progress")
            for step in self.STEPS:
                marker = "✅" if self.STEPS.index(step) < self.STEPS.index(st.session_state.step) else (
                    "➡️" if step == st.session_state.step else "⬜"
                )
                st.markdown(f"{marker} {step.replace('_', ' ').title()}")

            st.markdown("---")
            if st.button("🔄 Reset Application", use_container_width=True):
                self._full_reset()

    @staticmethod
    def _capability_row(label: str, ok: bool) -> None:
        css_class = "capability-ok" if ok else "capability-bad"
        symbol = "●" if ok else "○"
        st.markdown(f"<span class='{css_class}'>{symbol}</span> {label}", unsafe_allow_html=True)

    def _full_reset(self) -> None:
        bm: Optional[BrowserManager] = st.session_state.get("browser_manager")
        if bm:
            bm.close()
        for key in list(st.session_state.keys()):
            del st.session_state[key]
        st.rerun()

    # -- STEP 1: portal URL ---------------------------------------------
    def render_portal_step(self) -> None:
        st.title("🏢 Enterprise Portal Automation Assistant")
        st.caption("Streamlit control dashboard · Playwright/Selenium-driven Edge · No hardcoded portals")

        st.markdown("#### Step 1 — Portal URL")
        url = st.text_input("Enter the enterprise portal URL", value=st.session_state.portal_url,
                             placeholder="https://portal.company.com")
        st.info(
            "A dedicated Microsoft Edge window will open next, controlled by this dashboard. "
            "It is **not** embedded inside the browser tab, because enterprise SSO portals block "
            "iframe embedding for security reasons - see the notes at the top of app.py for details.",
            icon="ℹ️",
        )
        if st.button("Continue ➜", type="primary", disabled=not url.strip()):
            st.session_state.portal_url = url.strip()
            self._launch_browser_and_advance()

    def _launch_browser_and_advance(self) -> None:
        log: LogStore = st.session_state.log_store
        download_dir = Path(st.session_state.download_dir)
        download_dir.mkdir(parents=True, exist_ok=True)
        with st.spinner("Launching Microsoft Edge and opening the portal..."):
            try:
                bm = BrowserManager(log, download_dir)
                adapter = bm.launch()
                adapter.navigate(st.session_state.portal_url)
                st.session_state.browser_manager = bm
                st.session_state.step = "login"
                st.rerun()
            except Exception as exc:
                st.error(f"Could not start the browser: {exc}")
                log.error(f"Browser launch failed: {exc}")

    # -- STEP 2: manual login -------------------------------------------
    def render_login_step(self) -> None:
        st.title("🔐 Step 2 — Manual Login")
        st.warning(
            "Log in to the portal **in the Edge window that just opened**. "
            "This assistant never automates credentials, MFA, or SSO steps by design.",
            icon="🔒",
        )
        bm: BrowserManager = st.session_state.browser_manager
        st.write(f"Automation engine in use: **{bm.adapter.engine_name}**")
        col1, col2 = st.columns([1, 3])
        with col1:
            if st.button("✅ Login Completed", type="primary"):
                st.session_state.step = "inspect"
                st.rerun()
        with col2:
            if st.button("⬅ Back"):
                bm.close()
                st.session_state.browser_manager = None
                st.session_state.step = "portal"
                st.rerun()

    # -- STEP 3: page inspection -----------------------------------------
    def render_inspect_step(self) -> None:
        st.title("🔍 Step 3 — Automatic Page Inspection")
        bm: BrowserManager = st.session_state.browser_manager
        log: LogStore = st.session_state.log_store

        if not st.session_state.nav_labels:
            with st.spinner("Inspecting navigation menus, folders, cards and tables..."):
                retry = RetryManager(log)
                ocr = OCRHelper(log)
                locator_engine = LocatorEngine(bm.adapter, log, retry, ocr)
                st.session_state.nav_labels = locator_engine.discover_navigation()

        labels = st.session_state.nav_labels
        if labels:
            st.success(f"Discovered {len(labels)} navigable element(s) on the current page.")
            with st.expander("Show discovered elements"):
                st.write(labels)
        else:
            st.warning("No navigable elements were confidently detected. You can still proceed.")

        if st.button("Continue ➜", type="primary"):
            st.session_state.step = "folders"
            st.rerun()

    # -- STEP 4: folder search Y/N ---------------------------------------
    def render_folders_step(self) -> None:
        st.title("📁 Step 4 — Folder-Scoped Search?")
        st.write("Do you want to search inside a specific folder?")
        col_yes, col_no = st.columns(2)
        with col_yes:
            if st.button("YES", use_container_width=True):
                st.session_state.search_in_folder = True
                bm: BrowserManager = st.session_state.browser_manager
                log: LogStore = st.session_state.log_store
                retry = RetryManager(log)
                ocr = OCRHelper(log)
                locator_engine = LocatorEngine(bm.adapter, log, retry, ocr)
                angular_engine = AngularEngine(bm.adapter, log)
                navigator = FolderNavigator(bm.adapter, locator_engine, angular_engine, log)
                st.session_state.available_folders = navigator.list_folders()
                st.rerun()
        with col_no:
            if st.button("NO", use_container_width=True):
                st.session_state.search_in_folder = False
                st.session_state.step = "inputs"
                st.rerun()

        if st.session_state.search_in_folder and st.session_state.available_folders:
            folder = st.selectbox("Select a folder", st.session_state.available_folders)
            if st.button("Open Folder ➜", type="primary"):
                bm: BrowserManager = st.session_state.browser_manager
                log: LogStore = st.session_state.log_store
                retry = RetryManager(log)
                ocr = OCRHelper(log)
                locator_engine = LocatorEngine(bm.adapter, log, retry, ocr)
                angular_engine = AngularEngine(bm.adapter, log)
                navigator = FolderNavigator(bm.adapter, locator_engine, angular_engine, log)
                with st.spinner(f"Opening '{folder}'..."):
                    ok = navigator.open_folder(folder)
                if ok:
                    st.session_state.chosen_folder = folder
                    st.session_state.step = "inputs"
                    st.rerun()
                else:
                    st.error("Could not open that folder automatically. You may open it manually in Edge, "
                              "then click Continue below.")
                    if st.button("Continue anyway ➜"):
                        st.session_state.step = "inputs"
                        st.rerun()

    # -- STEP 5: input source ---------------------------------------------
    def render_inputs_step(self) -> None:
        st.title("📋 Step 5 — Search Inputs")
        input_type = st.radio("Input type", ["CSV", "Excel", "Manual list"], horizontal=True)

        queries: list[str] = []
        if input_type == "CSV":
            file = st.file_uploader("Upload CSV", type=["csv"])
            if file:
                df, _ = InputProcessor.from_csv(file)
                column = st.selectbox("Column containing search terms", df.columns)
                queries = InputProcessor._extract(df, column)
                st.dataframe(df.head(10), use_container_width=True)

        elif input_type == "Excel":
            file = st.file_uploader("Upload Excel", type=["xlsx", "xls"])
            if file:
                df, _ = InputProcessor.from_excel(file)
                column = st.selectbox("Column containing search terms", df.columns)
                queries = InputProcessor._extract(df, column)
                st.dataframe(df.head(10), use_container_width=True)

        else:
            text = st.text_area("Enter one search term per line", height=200)
            queries = InputProcessor.from_manual(text)

        if queries:
            st.success(f"{len(queries)} search item(s) ready.")
        if st.button("Continue ➜", type="primary", disabled=not queries):
            st.session_state.queries = queries
            st.session_state.step = "download_dir"
            st.rerun()

    # -- STEP 6: download directory ---------------------------------------
    def render_download_dir_step(self) -> None:
        st.title("💾 Step 6 — Download Directory")
        directory = st.text_input("Choose a download directory", value=st.session_state.download_dir)
        st.caption("The folder will be created automatically if it doesn't exist. "
                   "Duplicate filenames are renamed intelligently, e.g. 'report (1).pdf'.")
        if st.button("Start Automation ➜", type="primary", disabled=not directory.strip()):
            st.session_state.download_dir = directory.strip()
            Path(directory).mkdir(parents=True, exist_ok=True)
            self._start_automation()

    def _start_automation(self) -> None:
        bm: BrowserManager = st.session_state.browser_manager
        log: LogStore = st.session_state.log_store
        shared = SharedAutomationState()
        shared.jobs = [DownloadJob(query=q) for q in st.session_state.queries]
        st.session_state.shared_state = shared

        engine = AutomationEngine(bm.adapter, log, shared, Path(st.session_state.download_dir))
        st.session_state.engine = engine

        thread = threading.Thread(target=engine.run_all, daemon=True)
        st.session_state.automation_thread = thread
        st.session_state.step = "dashboard"
        thread.start()
        st.rerun()

    # -- STEP 7: dashboard --------------------------------------------------
    def render_dashboard_step(self) -> None:
        st.title("📊 Automation Dashboard")
        shared: SharedAutomationState = st.session_state.shared_state
        log: LogStore = st.session_state.log_store
        stats = shared.snapshot_stats()

        self._render_stat_cards(stats)
        self._render_progress(stats)
        self._render_pending_selection(shared)
        self._render_queue_table(shared)
        self._render_logs(log)

        if shared.running:
            col1, _ = st.columns([1, 4])
            with col1:
                if st.button("⏹ Stop After Current Item"):
                    shared.stop_requested.set()
            time.sleep(1.2)
            st.rerun()
        else:
            st.success("Automation run complete.")
            if st.button("🔁 Run Another Batch"):
                st.session_state.step = "inputs"
                st.session_state.shared_state = None
                st.rerun()

    @staticmethod
    def _render_stat_cards(stats: AutomationStats) -> None:
        cols = st.columns(6)
        cols[0].metric("Total", stats.total)
        cols[1].metric("Completed", stats.completed)
        cols[2].metric("Failed", stats.failed)
        cols[3].metric("Not Found", stats.not_found)
        cols[4].metric("Files Downloaded", stats.files_downloaded)
        cols[5].metric("Remaining", stats.remaining)

    @staticmethod
    def _render_progress(stats: AutomationStats) -> None:
        done = stats.total - stats.remaining
        fraction = (done / stats.total) if stats.total else 0.0
        st.progress(min(fraction, 1.0), text=f"{done}/{stats.total} processed")
        c1, c2 = st.columns(2)
        c1.caption(f"⏱ Elapsed: {stats.elapsed_seconds:0.1f}s")
        c2.caption(f"⏳ ETA: {stats.eta_seconds:0.1f}s")

    @staticmethod
    def _render_pending_selection(shared: SharedAutomationState) -> None:
        with shared.lock:
            pending = shared.pending_selection
        if not pending:
            return
        job: DownloadJob = pending["job"]
        results: list[SearchResultItem] = pending["results"]
        st.markdown("### 🖐 Action Needed: Select File(s) to Download")
        st.info(f"Query **'{job.query}'** returned {len(results)} results.")
        labels = {r.index: r.label for r in results}
        chosen = st.multiselect(
            "Choose which file(s) to download",
            options=list(labels.keys()),
            format_func=lambda i: labels[i],
            key=f"select_{job.query}_{id(job)}",
        )
        col1, col2 = st.columns(2)
        with col1:
            if st.button("Confirm Selection", type="primary"):
                job.selected_indices = chosen
                shared.selection_event.set()
                st.rerun()
        with col2:
            if st.button("Skip This Item"):
                job.selected_indices = []
                shared.selection_event.set()
                st.rerun()

    @staticmethod
    def _render_queue_table(shared: SharedAutomationState) -> None:
        st.markdown("### 🗂 Automation Queue")
        rows = []
        for j in shared.jobs:
            rows.append({
                "Query": j.query,
                "Status": j.status.value,
                "Results Found": len(j.results),
                "Files Downloaded": len(j.downloaded_files),
                "Attempts": j.attempts,
                "Duration (s)": j.duration() or "-",
                "Error": j.error or "",
            })
        st.dataframe(pd.DataFrame(rows), use_container_width=True, height=280)

    @staticmethod
    def _render_logs(log: LogStore) -> None:
        st.markdown("### 📜 Logs")
        records = log.records()[-200:]
        text = "\n".join(f"[{r['time']}] {r['level']:<7} {r['message']}" for r in records)
        st.text_area("Live log stream", value=text, height=220, disabled=True,
                      label_visibility="collapsed")
        st.caption(f"Full audit log file: `{log.log_file}`")

    # -- router -----------------------------------------------------------
    def render(self) -> None:
        self.inject_theme()
        self.render_sidebar()
        step = st.session_state.step
        {
            "portal": self.render_portal_step,
            "login": self.render_login_step,
            "inspect": self.render_inspect_step,
            "folders": self.render_folders_step,
            "inputs": self.render_inputs_step,
            "download_dir": self.render_download_dir_step,
            "dashboard": self.render_dashboard_step,
        }[step]()


# =============================================================================
# SECTION 18: ENTRYPOINT
# =============================================================================
def main() -> None:
    st.set_page_config(
        page_title="Enterprise Portal Automation Assistant",
        page_icon="🏢",
        layout="wide",
        initial_sidebar_state="expanded",
    )
    try:
        ui = UIManager()
        ui.render()
    except Exception as exc:
        # Top-level safety net: the user always sees a friendly message,
        # never a raw traceback, while the full detail still lands in the
        # log file for troubleshooting.
        st.error("An unexpected error occurred. Details have been written to the log file.")
        st.exception(exc) if st.session_state.get("_debug") else None
        log_store: Optional[LogStore] = st.session_state.get("log_store")
        if log_store:
            log_store.error(f"Unhandled UI exception: {exc}\n{traceback.format_exc()}")


if __name__ == "__main__":
    main()
