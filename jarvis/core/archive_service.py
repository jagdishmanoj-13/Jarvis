"""
core/archive_service.py
=========================

Expands `.zip` archives encountered during a folder scan so their
contents become indexable, without ever treating a `.zip` itself as a
text/parseable file.

Design decisions
-----------------
- Extraction target is `Settings.cache_dir/archives/<content_hash>/`, keyed
  by the zip's own content hash — identical to the caching strategy used
  everywhere else. Re-scanning an unchanged zip is then just as cheap as
  re-scanning an unchanged regular file: the extraction step is skipped
  entirely if that hash's directory already exists.
- **Zip-slip protection**: every member's resolved extraction path is
  checked to still be inside the target directory before writing. Without
  this, a maliciously crafted zip (`../../../Windows/System32/...` as an
  entry name) could write files outside the intended folder — a classic
  and well-known zip-handling vulnerability. Any archive containing such
  an entry is rejected outright rather than partially extracted.
- A total uncompressed-size cap (`_MAX_TOTAL_EXTRACTED_MB`) guards against
  zip bombs (a tiny compressed file that expands to gigabytes) filling the
  disk on a Citrix machine with a small, quota-limited user profile.
- Nested zips (a zip inside a zip) are intentionally NOT recursively
  expanded in this phase, to keep the size/complexity bound predictable;
  they will simply appear as an inert `.zip` file inside the extracted
  folder, itself registered as `UnavailableParser` for direct parsing.
- Returns the extraction directory path so `core.indexing_service` can
  call `index_folder()` on it exactly like any other folder — no special
  casing needed anywhere else in the pipeline.
"""

from __future__ import annotations

import zipfile
from pathlib import Path
from typing import Optional

from config.settings import get_settings
from utils.hashing import compute_file_hash
from utils.logger import get_logger

logger = get_logger(__name__)

_MAX_TOTAL_EXTRACTED_MB = 2048  # 2 GB cap guards against zip-bomb style archives


class ArchiveError(Exception):
    pass


def _is_safe_member_path(target_dir: Path, member_name: str) -> Optional[Path]:
    """Returns the resolved extraction path if it's safely inside
    target_dir, else None (zip-slip protection).
    """
    dest = (target_dir / member_name).resolve()
    try:
        dest.relative_to(target_dir.resolve())
    except ValueError:
        return None
    return dest


def expand_zip(zip_path: Path) -> Path:
    """Extracts `zip_path` into a content-hash-keyed cache directory and
    returns that directory. Idempotent: if already extracted (same
    content hash), returns the existing directory without re-extracting.
    """
    settings = get_settings()
    content_hash = compute_file_hash(zip_path)
    target_dir = settings.cache_dir / "archives" / content_hash

    if target_dir.exists() and any(target_dir.iterdir()):
        logger.debug("Archive %s already expanded at %s, skipping re-extraction", zip_path, target_dir)
        return target_dir

    target_dir.mkdir(parents=True, exist_ok=True)

    try:
        with zipfile.ZipFile(zip_path) as zf:
            total_uncompressed = sum(info.file_size for info in zf.infolist())
            if total_uncompressed > _MAX_TOTAL_EXTRACTED_MB * 1024 * 1024:
                raise ArchiveError(
                    f"{zip_path.name}: uncompressed size "
                    f"{total_uncompressed / (1024*1024):.0f} MB exceeds the "
                    f"{_MAX_TOTAL_EXTRACTED_MB} MB safety limit; skipped."
                )

            for info in zf.infolist():
                if info.is_dir():
                    continue
                dest = _is_safe_member_path(target_dir, info.filename)
                if dest is None:
                    raise ArchiveError(
                        f"{zip_path.name}: contains an unsafe path "
                        f"('{info.filename}') that would extract outside the "
                        f"target directory; entire archive skipped for safety."
                    )
                dest.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(info) as src, open(dest, "wb") as out:
                    out.write(src.read())
    except zipfile.BadZipFile as exc:
        raise ArchiveError(f"{zip_path.name}: not a valid zip file ({exc})")

    logger.info("Expanded archive %s -> %s", zip_path, target_dir)
    return target_dir
