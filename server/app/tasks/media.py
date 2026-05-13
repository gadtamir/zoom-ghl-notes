"""Media helpers: detect video, convert to audio."""

import logging
import shutil
import subprocess
from pathlib import Path


log = logging.getLogger(__name__)

VIDEO_EXTS = {".mp4", ".m4v", ".mov", ".avi", ".mkv", ".webm"}
AUDIO_EXTS = {".m4a", ".mp3", ".wav", ".aac", ".ogg", ".flac"}


def is_video(path: Path) -> bool:
    return path.suffix.lower() in VIDEO_EXTS


def is_audio(path: Path) -> bool:
    return path.suffix.lower() in AUDIO_EXTS


def ensure_ffmpeg() -> None:
    if shutil.which("ffmpeg") is None:
        raise RuntimeError("ffmpeg not found on PATH — install it (brew install ffmpeg / apt install ffmpeg)")


def video_to_audio(src: Path, dst: Path) -> Path:
    """Extract audio from video using ffmpeg. Returns dst path."""
    ensure_ffmpeg()
    dst.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "ffmpeg",
        "-y",
        "-loglevel", "error",
        "-i", str(src),
        "-vn",                  # drop video
        "-c:a", "aac",
        "-b:a", "96k",
        "-ac", "1",             # mono — fine for speech, smaller files
        "-ar", "16000",         # 16kHz — Whisper-optimal
        str(dst),
    ]
    log.info("ffmpeg start", extra={"src": str(src), "dst": str(dst)})
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg failed: {result.stderr.strip()[:500]}")
    log.info("ffmpeg done", extra={"dst": str(dst), "size_bytes": dst.stat().st_size})
    return dst
