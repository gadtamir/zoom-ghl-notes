"""Zoom cloud-recording pipeline: download → transcribe → summarize → note in GHL.

Triggered by the `recording.completed` webhook (see api/zoom_webhook.py). The
recording never touches an employee's machine — the worker pulls the audio-only
file straight from Zoom.

Deciding *whether* a meeting deserves a note and *who* it belongs to is the same
question: we look for an external participant that already exists as a GHL contact.
No such contact means it was an internal meeting, and we skip it rather than
polluting a customer's timeline.

Participant emails need a Server-to-Server OAuth app (the webhook payload doesn't
carry them). Until that exists we fall back to the contact name Claude extracts
from the transcript — the same signal the desktop-uploader flow has always used.
"""

import logging
from datetime import datetime
from pathlib import Path

from ..config import get_settings
from ..db import SessionLocal
from ..models import ZoomMeeting, ZoomMeetingStatus
from ..services.anthropic_client import summarize_meeting
from ..services.ghl_client import GHLClient, GHLError
from .celery_app import celery_app
from .transcribe import transcribe_audio


log = logging.getLogger(__name__)

MAX_RETRIES = 3
RETRY_BACKOFF_SEC = (60, 300, 900)
TERMINAL = (ZoomMeetingStatus.completed, ZoomMeetingStatus.skipped)


def _parse_zoom_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return None


def _internal_domains() -> set[str]:
    raw = get_settings().zoom_internal_domains or ""
    return {d.strip().lower() for d in raw.split(",") if d.strip()}


def _is_external(email: str | None) -> bool:
    if not email or "@" not in email:
        return False
    return email.rsplit("@", 1)[1].lower() not in _internal_domains()


def _storage_path(meeting_id: str) -> Path:
    return Path(get_settings().upload_dir) / f"zoom-{meeting_id}.m4a"


def _format_note(zm: ZoomMeeting) -> str:
    when = zm.started_at.strftime("%Y-%m-%d %H:%M") if zm.started_at else "?"
    host = zm.host_name or zm.host_email or "—"
    title = f"📞 סיכום פגישת זום - {when} ({zm.duration_minutes} דק') - {host}"
    return f"{title}\n\n{zm.summary or '(אין סיכום זמין)'}"


def _external_participant_emails(meeting_uuid: str) -> list[str]:
    """External attendee addresses, or [] when we can't ask Zoom (webhook-only app)."""
    settings = get_settings()
    if not (settings.zoom_account_id and settings.zoom_client_id and settings.zoom_client_secret):
        return []
    try:
        from ..services.zoom_client import ZoomClient

        with ZoomClient() as zoom:
            participants = zoom.past_meeting_participants(meeting_uuid)
    except Exception as exc:  # noqa: BLE001 — matching must never break the pipeline
        log.warning("could not fetch participants; falling back to name matching: %s", str(exc)[:200])
        return []
    seen: list[str] = []
    for p in participants:
        email = (p.get("user_email") or "").strip()
        if _is_external(email) and email.lower() not in [e.lower() for e in seen]:
            seen.append(email)
    return seen


def _find_contact(ghl: GHLClient, emails: list[str], fallback_name: str | None) -> tuple[str | None, str | None]:
    """(contact_id, matched_email). Email match wins; name is the fallback."""
    for email in emails:
        for c in ghl.search_contacts(query=email, limit=10):
            if (c.get("email") or "").strip().lower() == email.lower():
                return c.get("id"), email
    if fallback_name:
        contacts = ghl.search_contacts(query=fallback_name, limit=10)
        if len(contacts) == 1:
            return contacts[0].get("id"), None
        # Several matches on a bare name is a coin flip — better to skip than to
        # attach a customer's meeting summary to the wrong person.
        if len(contacts) > 1:
            log.warning("ambiguous name match, skipping", extra={"name": fallback_name, "n": len(contacts)})
    return None, None


@celery_app.task(name="zoom_meetings.process", bind=True, max_retries=MAX_RETRIES)
def process_zoom_recording(self, meeting: dict, download_token: str) -> dict:
    settings = get_settings()
    db = SessionLocal()
    audio_path: Path | None = None
    try:
        uuid = meeting.get("uuid") or ""
        zm = db.query(ZoomMeeting).filter(ZoomMeeting.zoom_meeting_uuid == uuid).first()
        if zm and zm.status in TERMINAL:
            # Zoom redelivers webhooks; a finished meeting is a no-op.
            return {"meeting": uuid, "status": "duplicate"}
        if not zm:
            zm = ZoomMeeting(
                zoom_meeting_uuid=uuid,
                zoom_meeting_id=str(meeting.get("id") or ""),
                topic=meeting.get("topic"),
                host_email=meeting.get("host_email"),
                started_at=_parse_zoom_time(meeting.get("start_time")),
                duration_minutes=int(meeting.get("duration") or 0),
                status=ZoomMeetingStatus.received,
            )
            db.add(zm)
        zm.attempts += 1
        db.commit()

        if zm.duration_minutes < settings.zoom_min_duration_minutes:
            zm.status = ZoomMeetingStatus.skipped
            zm.error_message = f"too short ({zm.duration_minutes}m)"
            zm.completed_at = datetime.utcnow()
            db.commit()
            log.info("zoom meeting skipped — too short", extra={"meeting": uuid})
            return {"meeting": uuid, "status": "skipped", "reason": "too_short"}

        audio = next(
            (f for f in meeting.get("recording_files", [])
             if f.get("recording_type") == "audio_only" and f.get("download_url")),
            None,
        )
        if not audio:
            zm.status = ZoomMeetingStatus.skipped
            zm.error_message = "no audio_only recording file"
            zm.completed_at = datetime.utcnow()
            db.commit()
            log.info("zoom meeting skipped — no audio file", extra={"meeting": uuid})
            return {"meeting": uuid, "status": "skipped", "reason": "no_audio"}

        # --- download (streamed to disk; the token is scoped to this recording) ---
        audio_path = _storage_path(zm.id)
        try:
            from ..services.zoom_client import download_with_token

            download_with_token(audio["download_url"], download_token, audio_path)
        except Exception as exc:
            return _retry_or_fail(self, db, zm, "download", exc)
        zm.status = ZoomMeetingStatus.downloaded
        db.commit()

        try:
            zm.transcript = transcribe_audio(audio_path, language="he")
            zm.status = ZoomMeetingStatus.transcribed
            db.commit()
        except Exception as exc:
            return _retry_or_fail(self, db, zm, "transcribe", exc)

        try:
            summary, extracted_name = summarize_meeting(zm.transcript or "", zm.host_name or "(לא ידוע)")
            zm.summary = summary
            zm.status = ZoomMeetingStatus.summarized
            db.commit()
        except Exception as exc:
            return _retry_or_fail(self, db, zm, "summarize", exc)

        # --- who was this with? no customer → internal meeting → skip ---
        try:
            emails = _external_participant_emails(uuid)
            with GHLClient() as ghl:
                contact_id, matched = _find_contact(ghl, emails, extracted_name)
                if not contact_id:
                    zm.status = ZoomMeetingStatus.skipped
                    zm.error_message = "no external GHL contact — treated as internal meeting"
                    zm.completed_at = datetime.utcnow()
                    db.commit()
                    log.info("zoom meeting skipped — internal", extra={"meeting": uuid, "topic": zm.topic})
                    return {"meeting": uuid, "status": "skipped", "reason": "internal"}
                zm.ghl_contact_id = contact_id
                zm.matched_email = matched
                note = ghl.create_note(contact_id=contact_id, body=_format_note(zm))
            zm.ghl_note_id = note.get("id")
            zm.status = ZoomMeetingStatus.completed
            zm.completed_at = datetime.utcnow()
            db.commit()
        except GHLError as exc:
            return _retry_or_fail(self, db, zm, "create_note", exc)

        log.info(
            "zoom meeting completed",
            extra={"meeting": uuid, "contact": contact_id, "note": zm.ghl_note_id, "matched_email": matched},
        )
        return {"meeting": uuid, "status": "completed", "note_id": zm.ghl_note_id}
    finally:
        if audio_path and audio_path.exists():
            try:
                audio_path.unlink()
            except OSError:
                pass
        db.close()


def _retry_or_fail(self, db, zm: ZoomMeeting, stage: str, exc: Exception) -> dict:
    zm.error_message = f"{stage}: {exc}"
    db.commit()
    log.exception("zoom stage failed", extra={"meeting": zm.zoom_meeting_uuid, "stage": stage})
    if self.request.retries < self.max_retries:
        delay = RETRY_BACKOFF_SEC[min(self.request.retries, len(RETRY_BACKOFF_SEC) - 1)]
        raise self.retry(exc=exc, countdown=delay)
    zm.status = ZoomMeetingStatus.failed
    zm.completed_at = datetime.utcnow()
    db.commit()
    return {"meeting": zm.zoom_meeting_uuid, "status": "failed", "error": f"{stage}: {exc}"}
