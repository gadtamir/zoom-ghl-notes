"""Transcribe stage: trim + compress, split if needed (streaming via ffmpeg), call OpenAI, concat."""

import logging
import subprocess
import tempfile
from pathlib import Path

from ..config import get_settings
from ..services.openai_client import USD_PER_AUDIO_MINUTE, transcribe_file, transcribe_model


log = logging.getLogger(__name__)

# OpenAI hard limit is 25 MB. We chunk well below to stay safe with re-encoding overhead.
_SAFE_CHUNK_MB = 20
_BYTES_PER_MB = 1024 * 1024
# gpt-4o-transcribe also rejects audio longer than 1400 s, whatever its size. Zoom's
# audio-only M4A is small enough that a 25-40 minute meeting stays under 20 MB, so
# the size check alone sent it whole and got a 400 every time.
_MAX_SINGLE_SECONDS = 1400
# If trimming removes more than this share of a recording, the threshold almost
# certainly caught quiet speech rather than silence — upload it untrimmed instead.
_MAX_TRIM_RATIO = 0.6


def model_for_meeting(topic: str | None) -> str | None:
    """The better transcription model for sales meetings, else None (the default)."""
    settings = get_settings()
    markers = [m.strip() for m in (settings.quality_transcribe_meeting_markers or "").split(",") if m.strip()]
    if topic and any(m in topic for m in markers):
        return settings.openai_quality_transcribe_model
    return None


def transcribe_audio(
    audio_path: Path, language: str = "he", context: dict | None = None, model: str | None = None,
) -> str:
    """Transcribe a recording of any length. `context` (e.g. {"call_job_id": ...})
    is attached to the per-job cost log line; `model` overrides the default model."""
    original_seconds = _probe_duration_seconds(audio_path)
    with tempfile.TemporaryDirectory(prefix="zghl-prep-") as tmpdir:
        upload_path, upload_seconds = _prepare_for_upload(audio_path, original_seconds, Path(tmpdir))
        text = _transcribe_prepared(upload_path, upload_seconds, language, model)

    model = model or transcribe_model()
    log.info(
        "transcription cost",
        extra={
            **(context or {}),
            "model": model,
            "audio_seconds": round(original_seconds, 1),
            "billed_seconds": round(upload_seconds, 1),
            "trimmed_seconds": round(original_seconds - upload_seconds, 1),
            "est_cost_usd": round(upload_seconds / 60 * USD_PER_AUDIO_MINUTE.get(model, 0.006), 4),
        },
    )
    return text


def _prepare_for_upload(src: Path, src_seconds: float, tmpdir: Path) -> tuple[Path, float]:
    """Re-encode to mono 16 kHz AAC (a phone WAV shrinks ~10x, cutting Render egress)
    and drop long silences (transcription is billed per minute). Never fatal: any
    ffmpeg trouble falls back to uploading the original file as-is."""
    settings = get_settings()
    dst = tmpdir / "upload.m4a"
    try:
        if settings.silence_trim_enabled:
            _encode(src, dst, trim=True)
            dst_seconds = _probe_duration_seconds(dst)
            if dst_seconds >= src_seconds * (1 - _MAX_TRIM_RATIO):
                return dst, dst_seconds
            log.warning(
                "silence trim removed too much — uploading untrimmed",
                extra={"audio_seconds": round(src_seconds, 1), "trimmed_to": round(dst_seconds, 1)},
            )
        _encode(src, dst, trim=False)
        return dst, _probe_duration_seconds(dst)
    except Exception as exc:  # noqa: BLE001 — an optimisation must never fail the job
        log.warning("audio prep failed — uploading original", extra={"path": str(src), "err": str(exc)[:300]})
        return src, src_seconds


def _encode(src: Path, dst: Path, trim: bool) -> None:
    settings = get_settings()
    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-i", str(src), "-vn"]
    if trim:
        cmd += [
            "-af",
            "silenceremove=stop_periods=-1"
            f":stop_duration={settings.silence_trim_min_seconds}"
            f":stop_threshold={settings.silence_trim_threshold_db}dB"
            ":stop_silence=0.5",
        ]
    cmd += ["-c:a", "aac", "-b:a", "64k", "-ac", "1", "-ar", "16000", str(dst)]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg prep failed: {result.stderr.strip()[:500]}")


def _transcribe_prepared(audio_path: Path, duration: float, language: str, model: str | None) -> str:
    size_bytes = audio_path.stat().st_size
    if size_bytes <= _SAFE_CHUNK_MB * _BYTES_PER_MB:
        if duration <= _MAX_SINGLE_SECONDS:
            return transcribe_file(audio_path, language=language, model=model)
        reason = "audio too long for single transcription"
    else:
        reason = "audio too large for single transcription"

    log.info(
        f"{reason} — chunking via ffmpeg",
        extra={"path": str(audio_path), "size_mb": round(size_bytes / _BYTES_PER_MB, 2)},
    )
    return _transcribe_chunked(audio_path, duration, language=language, model=model)


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


def _transcribe_chunked(audio_path: Path, duration: float, language: str, model: str | None) -> str:
    """Use ffmpeg's segment muxer to cut the audio without loading it into RAM.

    Output chunks are mono / 16 kHz / 96 kbps AAC — well-suited to speech
    and ~1.2 MB per minute, so a 10-minute chunk is ~12 MB (under the 20 MB
    safety threshold for OpenAI's 25 MB limit).
    """
    settings = get_settings()
    chunk_seconds = settings.transcript_chunk_minutes * 60
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
            parts.append(transcribe_file(chunk_path, language=language, model=model))

    return "\n\n".join(p for p in parts if p)
