"""OpenAI client — audio transcription only (model set by `openai_transcribe_model`)."""

import logging
from pathlib import Path

from openai import APIConnectionError, InternalServerError, RateLimitError, OpenAI
from tenacity import (
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

from ..config import get_settings


log = logging.getLogger(__name__)

# List price, USD per audio minute — for the per-job cost log only.
USD_PER_AUDIO_MINUTE = {
    "gpt-4o-transcribe": 0.006,
    "gpt-4o-mini-transcribe": 0.003,
    "whisper-1": 0.006,
}

_client: OpenAI | None = None


def transcribe_model() -> str:
    return get_settings().openai_transcribe_model


def _get_client() -> OpenAI:
    global _client
    if _client is None:
        settings = get_settings()
        if not settings.openai_api_key:
            raise RuntimeError("OPENAI_API_KEY not configured")
        # tenacity below owns retries; the SDK's own 2 retries would multiply them.
        _client = OpenAI(api_key=settings.openai_api_key, max_retries=0)
    return _client


def _is_transient(exc: BaseException) -> bool:
    """Connection, rate-limit and 5xx errors only. APIError also covers 400s, and
    retrying a request OpenAI has already rejected just re-uploads the same audio.
    An exhausted balance also arrives as a 429, but it won't clear in seconds."""
    if isinstance(exc, RateLimitError):
        return getattr(exc, "code", None) != "insufficient_quota"
    return isinstance(exc, (APIConnectionError, InternalServerError))


@retry(
    reraise=True,
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=2, min=2, max=30),
    retry=retry_if_exception(_is_transient),
)
def transcribe_file(path: Path, language: str = "he", model: str | None = None) -> str:
    """Transcribe a single audio file (< 25MB, < 1400 s).

    Retries 3x with exponential backoff on connection, rate-limit and 5xx errors.
    """
    client = _get_client()
    log.info("transcribe start", extra={"path": str(path), "size_mb": round(path.stat().st_size / 1024 / 1024, 2)})
    with path.open("rb") as f:
        result = client.audio.transcriptions.create(
            model=model or transcribe_model(),
            file=f,
            language=language,
            response_format="text",
        )
    text = result if isinstance(result, str) else getattr(result, "text", "")
    log.info("transcribe done", extra={"path": str(path), "chars": len(text)})
    return text.strip()
