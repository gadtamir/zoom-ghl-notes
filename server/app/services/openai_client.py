"""OpenAI client — currently only gpt-4o-transcribe (Whisper successor)."""

import logging
from pathlib import Path

from openai import APIError, APIConnectionError, RateLimitError, OpenAI
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from ..config import get_settings


log = logging.getLogger(__name__)

_TRANSCRIBE_MODEL = "gpt-4o-transcribe"

_client: OpenAI | None = None


def _get_client() -> OpenAI:
    global _client
    if _client is None:
        settings = get_settings()
        if not settings.openai_api_key:
            raise RuntimeError("OPENAI_API_KEY not configured")
        _client = OpenAI(api_key=settings.openai_api_key)
    return _client


@retry(
    reraise=True,
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=2, min=2, max=30),
    retry=retry_if_exception_type((APIConnectionError, RateLimitError, APIError)),
)
def transcribe_file(path: Path, language: str = "he") -> str:
    """Transcribe a single audio file (< 25MB) with gpt-4o-transcribe.

    Retries 3x with exponential backoff on transient API errors.
    """
    client = _get_client()
    log.info("transcribe start", extra={"path": str(path), "size_mb": round(path.stat().st_size / 1024 / 1024, 2)})
    with path.open("rb") as f:
        result = client.audio.transcriptions.create(
            model=_TRANSCRIBE_MODEL,
            file=f,
            language=language,
            response_format="text",
        )
    text = result if isinstance(result, str) else getattr(result, "text", "")
    log.info("transcribe done", extra={"path": str(path), "chars": len(text)})
    return text.strip()
