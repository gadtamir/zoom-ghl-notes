"""Transcribe stage: split if needed, call OpenAI, concat."""

import logging
import tempfile
from pathlib import Path

from pydub import AudioSegment

from ..config import get_settings
from ..services.openai_client import transcribe_file


log = logging.getLogger(__name__)

# OpenAI hard limit is 25 MB. We chunk well below that to stay safe with overhead.
_SAFE_CHUNK_MB = 20
_BYTES_PER_MB = 1024 * 1024


def transcribe_audio(audio_path: Path, language: str = "he") -> str:
    size_bytes = audio_path.stat().st_size
    if size_bytes <= _SAFE_CHUNK_MB * _BYTES_PER_MB:
        return transcribe_file(audio_path, language=language)

    log.info(
        "audio too large for single transcription — chunking",
        extra={"path": str(audio_path), "size_mb": round(size_bytes / _BYTES_PER_MB, 2)},
    )
    return _transcribe_chunked(audio_path, language=language)


def _transcribe_chunked(audio_path: Path, language: str) -> str:
    settings = get_settings()
    chunk_ms = settings.transcript_chunk_minutes * 60 * 1000

    audio = AudioSegment.from_file(audio_path)
    n_chunks = (len(audio) + chunk_ms - 1) // chunk_ms
    log.info("chunking", extra={"chunks": n_chunks, "minutes_each": settings.transcript_chunk_minutes})

    parts: list[str] = []
    with tempfile.TemporaryDirectory(prefix="zghl-chunks-") as tmpdir:
        for i in range(n_chunks):
            start = i * chunk_ms
            end = min((i + 1) * chunk_ms, len(audio))
            chunk = audio[start:end]
            chunk_path = Path(tmpdir) / f"chunk_{i:03d}.m4a"
            chunk.export(chunk_path, format="ipod", bitrate="96k", parameters=["-ac", "1", "-ar", "16000"])
            log.info("chunk ready", extra={"i": i, "size_mb": round(chunk_path.stat().st_size / _BYTES_PER_MB, 2)})
            parts.append(transcribe_file(chunk_path, language=language))

    return "\n\n".join(p for p in parts if p)
