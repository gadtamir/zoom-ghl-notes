"""Transcribe stage: split if needed (streaming via ffmpeg), call OpenAI, concat."""

import logging
import subprocess
import tempfile
from pathlib import Path

from ..config import get_settings
from ..services.openai_client import transcribe_file


log = logging.getLogger(__name__)

# OpenAI hard limit is 25 MB. We chunk well below to stay safe with re-encoding overhead.
_SAFE_CHUNK_MB = 20
_BYTES_PER_MB = 1024 * 1024


def transcribe_audio(audio_path: Path, language: str = "he") -> str:
    size_bytes = audio_path.stat().st_size
    if size_bytes <= _SAFE_CHUNK_MB * _BYTES_PER_MB:
        return transcribe_file(audio_path, language=language)

    log.info(
        "audio too large for single transcription — chunking via ffmpeg",
        extra={"path": str(audio_path), "size_mb": round(size_bytes / _BYTES_PER_MB, 2)},
    )
    return _transcribe_chunked(audio_path, language=language)


def _probe_duration_seconds(audio_path: Path) -> float:
    """ffprobe-style duration via ffmpeg. Streams the file — no RAM blow-up."""
    result = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            str(audio_path),
        ],
        capture_output=True, text=True, check=True,
    )
    return float(result.stdout.strip())


def _transcribe_chunked(audio_path: Path, language: str) -> str:
    """Use ffmpeg's segment muxer to cut the audio without loading it into RAM.

    Output chunks are mono / 16 kHz / 96 kbps AAC — well-suited to speech
    and ~1.2 MB per minute, so a 10-minute chunk is ~12 MB (under the 20 MB
    safety threshold for OpenAI's 25 MB limit).
    """
    settings = get_settings()
    chunk_seconds = settings.transcript_chunk_minutes * 60
    duration = _probe_duration_seconds(audio_path)
    n_chunks = max(1, int((duration + chunk_seconds - 1) // chunk_seconds))
    log.info(
        "ffmpeg segment split",
        extra={"duration_sec": round(duration, 1), "chunks": n_chunks, "minutes_each": settings.transcript_chunk_minutes},
    )

    parts: list[str] = []
    with tempfile.TemporaryDirectory(prefix="zghl-chunks-") as tmpdir:
        out_pattern = Path(tmpdir) / "chunk_%03d.m4a"
        cmd = [
            "ffmpeg", "-y", "-loglevel", "error",
            "-i", str(audio_path),
            "-vn",
            "-c:a", "aac", "-b:a", "96k",
            "-ac", "1", "-ar", "16000",
            "-f", "segment",
            "-segment_time", str(chunk_seconds),
            "-reset_timestamps", "1",
            str(out_pattern),
        ]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            raise RuntimeError(f"ffmpeg segment failed: {result.stderr.strip()[:500]}")

        chunk_files = sorted(Path(tmpdir).glob("chunk_*.m4a"))
        log.info("chunks ready", extra={"count": len(chunk_files)})

        for i, chunk_path in enumerate(chunk_files):
            size_mb = round(chunk_path.stat().st_size / _BYTES_PER_MB, 2)
            log.info("transcribing chunk", extra={"i": i, "size_mb": size_mb})
            parts.append(transcribe_file(chunk_path, language=language))

    return "\n\n".join(p for p in parts if p)
