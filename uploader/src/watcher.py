"""Scan the Zoom recordings folder, identify uploadable recordings.

Zoom default layout on macOS / Windows:
    <Zoom folder>/
        2026-05-13 11.30.45 Topic/
            audio_only.m4a
            video1080p.mp4
            playback.m3u
        ...

We pick **one file per meeting folder** — preferring audio_only.m4a when present
(smallest, audio-only, lossless for our purposes).

A folder is considered "ready to upload" when:
  - It contains a usable audio/video file
  - No file in the folder has been modified in the last `min_idle_seconds`
    (this ensures Zoom finished writing)
"""

import re
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


AUDIO_PREFERENCE = ["audio_only.m4a", "audio_only.mp3"]
ALL_EXTENSIONS = {".m4a", ".mp3", ".mp4", ".m4v", ".mov", ".wav"}

# folders we should never scan (our own + Zoom's transient ones)
SKIP_DIRS = {"uploaded", "skipped", "double_click_to_convert_01.zoom"}

_DATE_PREFIX = re.compile(r"^(\d{4}-\d{2}-\d{2})")


@dataclass
class Recording:
    folder: Path
    file: Path
    folder_name: str    # the topic — folder basename
    meeting_date: str   # YYYY-MM-DD extracted from folder name, or "" if absent


def _extract_date(folder_name: str) -> str:
    m = _DATE_PREFIX.search(folder_name)
    return m.group(1) if m else ""


def _pick_best_file(folder: Path) -> Path | None:
    """Prefer audio_only.m4a; otherwise the largest media file."""
    for name in AUDIO_PREFERENCE:
        cand = folder / name
        if cand.is_file():
            return cand
    media = [p for p in folder.iterdir() if p.is_file() and p.suffix.lower() in ALL_EXTENSIONS]
    if not media:
        return None
    media.sort(key=lambda p: p.stat().st_size, reverse=True)
    return media[0]


def _folder_is_idle(folder: Path, min_idle_seconds: int) -> bool:
    now = time.time()
    for p in folder.iterdir():
        if not p.is_file():
            continue
        if now - p.stat().st_mtime < min_idle_seconds:
            return False
    return True


def scan(watch_folder: Path, min_idle_seconds: int = 60) -> list[Recording]:
    """Return Recording entries that look ready to upload.

    Doesn't filter against the DB — the caller does that (so we keep watcher pure).
    """
    out: list[Recording] = []
    if not watch_folder.exists() or not watch_folder.is_dir():
        return out

    for entry in sorted(watch_folder.iterdir()):
        if not entry.is_dir():
            continue
        if entry.name in SKIP_DIRS or entry.name.startswith("."):
            continue
        if not _folder_is_idle(entry, min_idle_seconds):
            continue
        best = _pick_best_file(entry)
        if not best:
            continue
        out.append(
            Recording(
                folder=entry,
                file=best,
                folder_name=entry.name,
                meeting_date=_extract_date(entry.name) or datetime.now().strftime("%Y-%m-%d"),
            )
        )
    return out
