"""scanner.py — MediaFile dataclass, filesystem walk, Jellyfin API, ffprobe."""

from __future__ import annotations

import json
import logging
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterator, List, Optional

logger = logging.getLogger(__name__)

FFPROBE_MISSING_WARNED = False


@dataclass
class MediaFile:
    path: Path
    size: int
    stem: str
    jellyfin_id: Optional[str] = None
    provider_ids: Dict[str, str] = field(default_factory=dict)
    width: int = 0
    height: int = 0
    bitrate: int = 0
    probed: bool = False

    @property
    def resolution_score(self) -> int:
        return self.width * self.height

    @property
    def quality_key(self) -> tuple:
        """Higher is better."""
        return (self.resolution_score, self.bitrate, self.size)

    def __hash__(self):
        return hash(self.path)

    def __eq__(self, other):
        return isinstance(other, MediaFile) and self.path == other.path


# ---------------------------------------------------------------------------
# Filesystem walk
# ---------------------------------------------------------------------------

_RECENTLY_MODIFIED_SECS = 300  # 5 minutes


def walk_paths(paths: List[str], extensions: List[str]) -> Iterator[MediaFile]:
    """Yield MediaFile objects for every media file found under the given paths."""
    exts = {f".{e.lower().lstrip('.')}" for e in extensions}
    now = time.time()

    for root_str in paths:
        root = Path(root_str)
        if not root.exists():
            logger.warning("Path does not exist, skipping: %s", root)
            continue

        for p in root.rglob("*"):
            try:
                if not p.is_file():
                    continue
                # Skip symlinks — not real duplicates
                if p.is_symlink():
                    logger.debug("Skipping symlink: %s", p)
                    continue
                if p.suffix.lower() not in exts:
                    continue
                stat = p.lstat()
                # Skip zero-byte files
                if stat.st_size == 0:
                    logger.debug("Skipping zero-byte file: %s", p)
                    continue
                # Skip recently modified (incomplete downloads)
                if now - stat.st_mtime < _RECENTLY_MODIFIED_SECS:
                    logger.info("Skipping recently modified file: %s", p)
                    continue
                yield MediaFile(path=p, size=stat.st_size, stem=p.stem)
            except PermissionError as exc:
                logger.warning("Permission error reading %s: %s", p, exc)
            except OSError as exc:
                logger.warning("OS error reading %s: %s", p, exc)


# ---------------------------------------------------------------------------
# Jellyfin API
# ---------------------------------------------------------------------------

_PAGE_LIMIT = 500


def enrich_from_jellyfin(
    files: List[MediaFile],
    url: str,
    api_key: str,
    timeout: int = 10,
) -> None:
    """Populate jellyfin_id and provider_ids on MediaFile objects in-place."""
    if not url or not api_key:
        logger.info("Jellyfin URL or API key not set; skipping API enrichment.")
        return

    path_map: Dict[str, MediaFile] = {str(mf.path): mf for mf in files}

    start = 0
    while True:
        params = urllib.parse.urlencode(
            {
                "Recursive": "true",
                "IncludeItemTypes": "Movie,Episode",
                "Fields": "ProviderIds,Path",
                "Limit": str(_PAGE_LIMIT),
                "StartIndex": str(start),
                "apikey": api_key,
            }
        )
        req_url = f"{url.rstrip('/')}/Items?{params}"
        try:
            with urllib.request.urlopen(req_url, timeout=timeout) as resp:
                data = json.loads(resp.read())
        except (urllib.error.URLError, OSError) as exc:
            logger.warning("Jellyfin API unreachable: %s — falling back to filesystem-only mode.", exc)
            return
        except json.JSONDecodeError as exc:
            logger.warning("Jellyfin API returned invalid JSON: %s", exc)
            return

        items = data.get("Items", [])
        if not items:
            break

        for item in items:
            item_path = item.get("Path", "")
            if item_path in path_map:
                mf = path_map[item_path]
                mf.jellyfin_id = item.get("Id")
                raw_ids = item.get("ProviderIds") or {}
                mf.provider_ids = {k.lower(): v for k, v in raw_ids.items()}

        start += _PAGE_LIMIT
        if start >= data.get("TotalRecordCount", 0):
            break

    enriched = sum(1 for mf in files if mf.jellyfin_id)
    logger.info("Jellyfin API enriched %d / %d files.", enriched, len(files))


# ---------------------------------------------------------------------------
# ffprobe quality analysis
# ---------------------------------------------------------------------------

def _ffprobe_available() -> bool:
    try:
        subprocess.run(
            ["ffprobe", "-version"],
            capture_output=True,
            timeout=5,
        )
        return True
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def probe_files(files: List[MediaFile]) -> None:
    """Run ffprobe on each file to fill in width/height/bitrate."""
    global FFPROBE_MISSING_WARNED

    if not files:
        return

    if not _ffprobe_available():
        if not FFPROBE_MISSING_WARNED:
            from rich.console import Console as _Console
            _Console().print(
                "[yellow]WARNING:[/] ffprobe not found. "
                "Install ffmpeg for quality-based ranking "
                "(macOS: [bold]brew install ffmpeg[/], Linux: [bold]apt install ffmpeg[/]).\n"
                "Falling back to file-size ranking."
            )
            FFPROBE_MISSING_WARNED = True
        return

    for mf in files:
        if mf.probed:
            continue
        if mf.size == 0:
            mf.probed = True
            continue
        _probe_one(mf)


def _probe_one(mf: MediaFile) -> None:
    try:
        result = subprocess.run(
            [
                "ffprobe",
                "-v", "quiet",
                "-print_format", "json",
                "-show_streams",
                "-show_format",
                str(mf.path),
            ],
            capture_output=True,
            timeout=30,
        )
        if result.returncode != 0:
            logger.debug("ffprobe non-zero exit for %s", mf.path)
            mf.probed = True
            return

        data = json.loads(result.stdout)
        # Extract video stream info
        for stream in data.get("streams", []):
            if stream.get("codec_type") == "video":
                mf.width = int(stream.get("width") or 0)
                mf.height = int(stream.get("height") or 0)
                break

        # Bitrate from format
        fmt = data.get("format", {})
        try:
            mf.bitrate = int(float(fmt.get("bit_rate") or 0))
        except (ValueError, TypeError):
            mf.bitrate = 0

        mf.probed = True
    except (subprocess.TimeoutExpired, json.JSONDecodeError, OSError) as exc:
        logger.debug("ffprobe error for %s: %s", mf.path, exc)
        mf.probed = True
