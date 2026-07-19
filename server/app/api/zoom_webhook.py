"""Zoom webhook receiver — turns `recording.completed` events into transcription jobs.

Zoom signs every request with HMAC-SHA256 over `v0:{timestamp}:{raw body}` using the
app's Secret Token, so we verify the raw bytes before trusting anything. The same
secret answers Zoom's one-off `endpoint.url_validation` challenge when the URL is
first saved in the Marketplace.

The handler does no real work: it validates, records the job, and hands off to Celery.
Zoom retries on non-2xx and gives up after a few attempts, so responding fast matters
more than responding completely.
"""

import hashlib
import hmac
import json
import logging
import time

from fastapi import APIRouter, HTTPException, Request, status

from ..config import get_settings


log = logging.getLogger(__name__)

router = APIRouter(prefix="/webhooks", tags=["webhooks"])

# Reject events whose timestamp is older than this — a replayed request beyond the
# window is not something we want to act on.
MAX_SKEW_SEC = 5 * 60


def _signature(secret: str, timestamp: str, raw_body: bytes) -> str:
    message = b"v0:" + timestamp.encode() + b":" + raw_body
    digest = hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()
    return f"v0={digest}"


@router.post("/zoom", status_code=status.HTTP_200_OK)
async def zoom_webhook(request: Request) -> dict:
    settings = get_settings()
    secret = settings.zoom_webhook_secret_token
    if not secret:
        # Fail closed: an unconfigured endpoint must not accept anything.
        log.error("zoom webhook hit but ZOOM_WEBHOOK_SECRET_TOKEN is not set")
        raise HTTPException(status_code=503, detail="zoom webhook not configured")

    raw = await request.body()
    try:
        payload = json.loads(raw)
    except ValueError:
        raise HTTPException(status_code=400, detail="invalid JSON")

    event = payload.get("event")

    # --- one-off URL validation when the endpoint is saved in the Marketplace ---
    if event == "endpoint.url_validation":
        plain = (payload.get("payload") or {}).get("plainToken", "")
        encrypted = hmac.new(secret.encode(), plain.encode(), hashlib.sha256).hexdigest()
        log.info("zoom url_validation answered")
        return {"plainToken": plain, "encryptedToken": encrypted}

    # --- every other event must carry a valid signature ---
    timestamp = request.headers.get("x-zm-request-timestamp", "")
    signature = request.headers.get("x-zm-signature", "")
    if not timestamp or not signature:
        raise HTTPException(status_code=401, detail="missing signature headers")
    try:
        if abs(time.time() - int(timestamp)) > MAX_SKEW_SEC:
            raise HTTPException(status_code=401, detail="stale request")
    except ValueError:
        raise HTTPException(status_code=401, detail="bad timestamp")
    if not hmac.compare_digest(_signature(secret, timestamp, raw), signature):
        log.warning("zoom webhook signature mismatch", extra={"event": event})
        raise HTTPException(status_code=401, detail="bad signature")

    if event != "recording.completed":
        # Subscribed to something we don't handle — ack so Zoom stops retrying.
        log.info("zoom event ignored", extra={"event": event})
        return {"status": "ignored", "event": event}

    obj = (payload.get("payload") or {}).get("object") or {}
    meeting_uuid = obj.get("uuid")
    if not meeting_uuid:
        log.warning("recording.completed without a meeting uuid")
        return {"status": "ignored", "reason": "no uuid"}

    # Zoom ships a short-lived token that authorizes downloading THIS recording's
    # files — this is what lets a Webhook-Only app fetch the audio with no OAuth app.
    download_token = payload.get("download_token", "")

    # Imported here so the module graph stays flat and the API can boot even if
    # Celery/broker config is unavailable at import time.
    from ..tasks.zoom_meetings import process_zoom_recording

    process_zoom_recording.delay(obj, download_token)
    log.info(
        "zoom recording queued",
        extra={
            "meeting_uuid": meeting_uuid,
            "topic": obj.get("topic"),
            "host_email": obj.get("host_email"),
            "duration": obj.get("duration"),
        },
    )
    return {"status": "queued"}
