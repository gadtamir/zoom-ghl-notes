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
import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from pathlib import Path

from ..config import get_settings
from ..db import SessionLocal
from ..models import ZoomMeeting, ZoomMeetingStatus
from ..services.anthropic_client import summarize_meeting
from ..services.ghl_client import GHLClient, GHLError
from .celery_app import celery_app
from .ghl import _score_by_appointment, _split_topic_into_candidates
from .spec_stage import build_and_deliver_to_ghl, build_bot_and_deliver_to_ghl, is_spec_meeting
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


def _internal_emails() -> set[str]:
    raw = get_settings().zoom_internal_emails or ""
    return {e.strip().lower() for e in raw.split(",") if e.strip()}


def _is_external(email: str | None) -> bool:
    """True only for an address that is neither on a company domain nor a known teammate.

    The team signs in with personal Gmail addresses, so a domain check alone marks
    colleagues as customers — and the note lands on a teammate's GHL card.
    """
    if not email or "@" not in email:
        return False
    email = email.strip().lower()
    if email in _internal_emails():
        return False
    return email.rsplit("@", 1)[1] not in _internal_domains()


def _storage_path(meeting_id: str) -> Path:
    return Path(get_settings().upload_dir) / f"zoom-{meeting_id}.m4a"


def _format_note(zm: ZoomMeeting) -> str:
    when = zm.started_at.strftime("%Y-%m-%d %H:%M") if zm.started_at else "?"
    host = zm.host_name or zm.host_email or "—"
    title = f"📞 סיכום פגישת זום - {when} ({zm.duration_minutes} דק') - {host}"
    return f"{title}\n\n{zm.summary or '(אין סיכום זמין)'}"


# GHL rejects a note body over 65,000 characters ("body must be shorter than or
# equal to 65000 characters"). Leave room for the title/part header we prepend.
NOTE_MAX_CHARS = 65000
TRANSCRIPT_BODY_BUDGET = NOTE_MAX_CHARS - 500


def _paragraphize(text: str) -> str:
    """Break a wall of transcript into readable paragraphs.

    The transcriber returns continuous prose with no speaker labels, which is
    painful to skim in a CRM card. Rather than pay an LLM to reformat, group a
    few sentences at a time — enough structure to scan, no risk of altering what
    was actually said.
    """
    parts = re.split(r"(?<=[.!?…])\s+", (text or "").strip())
    sentences = [p.strip() for p in parts if p.strip()]
    if not sentences:
        return (text or "").strip()
    paragraphs, buf = [], []
    for s in sentences:
        buf.append(s)
        if len(buf) >= 4:
            paragraphs.append(" ".join(buf))
            buf = []
    if buf:
        paragraphs.append(" ".join(buf))
    return "\n\n".join(paragraphs)


def _split_for_notes(body: str, budget: int = TRANSCRIPT_BODY_BUDGET) -> list[str]:
    """Chunk text to fit GHL's per-note limit, breaking on paragraph boundaries.

    A single paragraph longer than the budget (rare, but possible when the
    transcriber emits no sentence punctuation at all) is hard-split so we never
    loop forever or exceed the limit.
    """
    if len(body) <= budget:
        return [body]
    chunks: list[str] = []
    current = ""
    for para in body.split("\n\n"):
        while len(para) > budget:
            if current:
                chunks.append(current)
                current = ""
            chunks.append(para[:budget])
            para = para[budget:]
        candidate = f"{current}\n\n{para}" if current else para
        if len(candidate) > budget:
            chunks.append(current)
            current = para
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks


def _transcript_notes(zm: ZoomMeeting) -> list[str]:
    """Full-transcript note bodies to attach after the summary (empty if none)."""
    if not (zm.transcript or "").strip():
        return []
    when = zm.started_at.strftime("%Y-%m-%d %H:%M") if zm.started_at else "?"
    chunks = _split_for_notes(_paragraphize(zm.transcript))
    total = len(chunks)
    bodies = []
    for i, chunk in enumerate(chunks, start=1):
        suffix = f" (חלק {i}/{total})" if total > 1 else ""
        bodies.append(f"📝 תמלול מלא - פגישת זום {when}{suffix}\n\n{chunk}")
    return bodies


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


def _israel_local(dt_utc: datetime | None) -> datetime | None:
    """Zoom reports UTC; GHL stores appointment times as naive Israel local time.

    Compared naively, every match would be off by the UTC offset — and hardcoding
    +3 breaks for half the year, so convert through the tz database (IST/IDT).
    """
    if dt_utc is None:
        return None
    return (
        dt_utc.replace(tzinfo=timezone.utc)
        .astimezone(ZoneInfo("Asia/Jerusalem"))
        .replace(tzinfo=None)
    )


_ZOOM_ID_IN_URL = re.compile(r"/j/(\d+)")


def _match_by_calendar(
    ghl: GHLClient, host_email: str | None, zoom_meeting_id: str | None, started_at: datetime | None
) -> str | None:
    """Contact id from the GHL booking behind this recording, or None.

    Strongest signal we have: a meeting booked through GHL creates a calendar
    event that stores this exact Zoom meeting id and points straight at the
    contact — so "דנה", ambiguous across ten cards by name, resolves to one.
    We look at the host's own calendar, matching first on the embedded Zoom id
    and only then, if the URL can't be read, on the closest event by time.
    """
    if not (zoom_meeting_id and started_at):
        return None
    # started_at is UTC (naive); epoch is absolute, so no tz juggling is needed
    # for the query window or the fallback delta.
    center = int(started_at.replace(tzinfo=timezone.utc).timestamp() * 1000)
    window_ms = 12 * 60 * 60 * 1000
    try:
        users = ghl.list_users()
    except Exception as exc:  # noqa: BLE001 — matching must never break the pipeline
        log.warning("calendar match failed, falling back: %s", str(exc)[:200])
        return None

    # The Zoom meeting id is globally unique but the booking lives on whichever
    # teammate's calendar owns the appointment — not necessarily the one who
    # hosted the recording. So scan the team, host first (the common case, so it
    # usually resolves on the first call), then the rest.
    def _rank(u: dict) -> int:
        same = host_email and (u.get("email") or "").strip().lower() == host_email.strip().lower()
        return 0 if same else 1

    host_events: list[dict] = []
    for u in sorted(users, key=_rank):
        uid = u.get("id")
        if not uid:
            continue
        try:
            events = ghl.calendar_events(uid, center - window_ms, center + window_ms)
        except Exception as exc:  # noqa: BLE001
            log.warning("calendar_events failed for one user: %s", str(exc)[:150])
            continue
        for e in events:
            m = _ZOOM_ID_IN_URL.search(e.get("address") or "")
            if m and m.group(1) == str(zoom_meeting_id) and e.get("contactId"):
                return e["contactId"]
        if _rank(u) == 0:
            host_events = events  # keep for the time-based fallback below

    # No id match anywhere (URL edited/missing). Fall back to the single closest
    # event, but only on the host's own calendar — a same-time event on someone
    # else's calendar would be a coincidence, not this meeting.
    best_id, best_delta = None, None
    for e in host_events:
        try:
            ev_ms = datetime.fromisoformat(e["startTime"]).timestamp() * 1000
        except (KeyError, ValueError):
            continue
        if not e.get("contactId"):
            continue
        delta = abs(ev_ms - center)
        if delta <= 30 * 60 * 1000 and (best_delta is None or delta < best_delta):
            best_id, best_delta = e["contactId"], delta
    return best_id


def _find_contact(
    ghl: GHLClient,
    emails: list[str],
    topic: str | None,
    started_at: datetime | None,
    fallback_name: str | None,
    host_email: str | None = None,
    zoom_meeting_id: str | None = None,
) -> tuple[str | None, str | None]:
    """(contact_id, matched_email) — strongest available signal wins.

    Order: (1) the GHL booking behind the recording, matched by the Zoom meeting
    id it stores — unambiguous, and the case that actually holds for scheduled
    meetings; (2) a participant email, when Zoom reports one (it doesn't for the
    guests that customers join as); (3) the topic name, ties broken by GHL
    appointment time; (4) the name Claude pulled from the transcript, a guess of
    last resort. A bare first name ("דנה") matches ten contacts, so it is never
    trusted on its own without the calendar or an appointment to disambiguate.
    """
    # Strongest signal first: the GHL booking behind this recording names the
    # contact outright, so a meeting scheduled through the calendar never depends
    # on the name being unique.
    contact_id = _match_by_calendar(ghl, host_email, zoom_meeting_id, started_at)
    if contact_id:
        return contact_id, None

    for email in emails:
        for c in ghl.search_contacts(query=email, limit=10):
            if (c.get("email") or "").strip().lower() == email.lower():
                return c.get("id"), email

    names = _split_topic_into_candidates(topic)
    if fallback_name and fallback_name not in names:
        names.append(fallback_name)

    meeting_dt = _israel_local(started_at)
    ambiguous: dict[str, dict] = {}
    for name in names:
        contacts = ghl.search_contacts(query=name, limit=10)
        if len(contacts) == 1:
            return contacts[0].get("id"), None
        for c in contacts:
            if c.get("id") and c["id"] not in ambiguous:
                ambiguous[c["id"]] = {"contact": c, "matched_by": name}

    if ambiguous and meeting_dt:
        cid, delta = _score_by_appointment(ghl, ambiguous, meeting_dt, "zoom")
        if cid:
            log.info(
                "matched by appointment window",
                extra={"contact_id": cid, "delta_minutes": int(delta.total_seconds() // 60)},
            )
            return cid, None

    if ambiguous:
        # Several matches and no appointment to break the tie is a coin flip —
        # better to skip than to attach a meeting summary to the wrong customer.
        log.warning("ambiguous match with no appointment, skipping", extra={"n": len(ambiguous)})
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
        # A freshly-constructed row hasn't been flushed yet, so the column
        # default (0) has not been applied and zm.attempts is still None here —
        # `None += 1` is what crashed the very first real recording. Coalesce.
        zm.attempts = (zm.attempts or 0) + 1
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
            # Zoom finishes the audio-only (M4A) track a while after
            # `recording.completed` fires, so a missing audio file is almost always
            # "not ready yet", not "never coming". Skipping *terminally* here is the
            # bug that made the safety-net poller ignore the meeting forever once the
            # M4A did land. Instead leave it non-terminal so the poller re-enqueues it
            # on its next sweep (by then the file is there), and only give up — a
            # genuinely audio-less recording — after enough attempts.
            if zm.attempts < settings.zoom_audio_wait_max_attempts:
                zm.status = ZoomMeetingStatus.received
                zm.error_message = f"audio_only not ready yet (attempt {zm.attempts})"
                db.commit()
                log.info("zoom meeting deferred — audio not ready",
                         extra={"meeting": uuid, "attempts": zm.attempts})
                return {"meeting": uuid, "status": "deferred", "reason": "audio_not_ready"}
            zm.status = ZoomMeetingStatus.skipped
            zm.error_message = "no audio_only recording file after retries"
            zm.completed_at = datetime.utcnow()
            db.commit()
            log.info("zoom meeting skipped — no audio file after retries",
                     extra={"meeting": uuid, "attempts": zm.attempts})
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
            summary, extracted_name = summarize_meeting(zm.transcript or "", zm.host_name or "(לא ידוע)", zm.topic)
            zm.summary = summary
            zm.status = ZoomMeetingStatus.summarized
            db.commit()
        except Exception as exc:
            return _retry_or_fail(self, db, zm, "summarize", exc)

        # --- who was this with? no customer → internal meeting → skip ---
        try:
            emails = _external_participant_emails(uuid)
            with GHLClient() as ghl:
                contact_id, matched = _find_contact(ghl, emails, zm.topic, zm.started_at, extracted_name,
                                              host_email=zm.host_email, zoom_meeting_id=zm.zoom_meeting_id)
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
                # The full transcript follows the summary as its own note(s), so
                # the readable summary stays the first thing on the card. Best
                # effort: the summary is the deliverable, and losing the raw
                # transcript must never fail an otherwise-successful meeting.
                for body in _transcript_notes(zm):
                    try:
                        ghl.create_note(contact_id=contact_id, body=body)
                    except Exception as exc:  # noqa: BLE001
                        log.warning(
                            "transcript note failed — summary already attached",
                            extra={"meeting": uuid, "contact": contact_id, "err": str(exc)[:200]},
                        )
                        break

                # --- discovery meeting? build the spec doc + push the PDF into GHL.
                # Best-effort and self-contained: any failure (incl. a GHL write
                # error) is swallowed here so it never reaches the outer GHLError
                # handler, which would retry the whole meeting and double the notes.
                if settings.spec_builder_enabled and is_spec_meeting(zm.topic):
                    log.info("spec meeting — building spec + bot prompt, attaching PDFs to GHL",
                             extra={"meeting": uuid, "contact": contact_id})
                    date = (zm.started_at or datetime.utcnow()).strftime("%Y-%m-%d")
                    client_name = extracted_name or None
                    # Two docs, each self-contained and best-effort: a failure in
                    # one must not stop the other, and neither may reach the outer
                    # GHLError handler (which would retry and double the summary).
                    builders = (
                        ("spec", lambda: build_and_deliver_to_ghl(
                            ghl, contact_id, zm.transcript or "", client_name=client_name,
                            employee_name=zm.host_name, date=date)),
                        ("bot", lambda: build_bot_and_deliver_to_ghl(
                            ghl, contact_id, zm.transcript or "", client_name=client_name, date=date)),
                    )
                    for label, build in builders:
                        try:
                            res = build()
                            if res.get("note_addition"):
                                ghl.create_note(contact_id=contact_id, body=res["note_addition"])
                            if not res.get("ok"):
                                log.warning("%s build incomplete", label,
                                            extra={"meeting": uuid, "error": res.get("error")})
                        except Exception as exc:  # noqa: BLE001
                            log.warning("%s stage failed — summary already attached", label,
                                        extra={"meeting": uuid, "err": str(exc)[:200]})
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


@celery_app.task(name="zoom_meetings.poll")
def poll_recordings() -> dict:
    """Safety net: pull recent cloud recordings and enqueue any we haven't done.

    The webhook is the primary path, but a dropped delivery or a worker that dies
    mid-task (as happened on the very first real recording) leaves a recording
    that no event will ever re-announce — Zoom considers a 200-acked webhook done.
    This sweep re-discovers those: it lists the last `zoom_poll_lookback_hours` and
    enqueues anything with no terminal row yet. `process_zoom_recording` dedupes on
    the meeting uuid, so re-enqueuing one already in flight is harmless.

    No webhook download_token here, so we pass an OAuth access token instead — it
    authorises the same file download for an account-level app.
    """
    settings = get_settings()
    if not (settings.zoom_account_id and settings.zoom_client_id and settings.zoom_client_secret):
        return {"status": "no_credentials"}

    from ..services.zoom_client import ZoomClient

    since = datetime.utcnow() - timedelta(hours=settings.zoom_poll_lookback_hours)
    db = SessionLocal()
    queued = 0
    try:
        with ZoomClient() as zoom:
            meetings = zoom.list_recordings(since=since)
            token = zoom._access_token()
        done = {
            row.zoom_meeting_uuid
            for row in db.query(ZoomMeeting.zoom_meeting_uuid)
            .filter(ZoomMeeting.status.in_(TERMINAL))
            .all()
        }
        for m in meetings:
            uuid = m.get("uuid") or ""
            if not uuid or uuid in done:
                continue
            process_zoom_recording.delay(m, token)
            queued += 1
        log.info(
            "zoom poll done",
            extra={"listed": len(meetings), "queued": queued, "lookback_hours": settings.zoom_poll_lookback_hours},
        )
        return {"status": "ok", "listed": len(meetings), "queued": queued}
    finally:
        db.close()


@celery_app.task(name="zoom_meetings.cleanup_recordings")
def cleanup_recordings() -> dict:
    """Delete Zoom cloud recordings we've finished with, after a retention window.

    Not deleted on completion: the team may want to keep a recording (a closed
    deal, say), so there's a grace period to save it first. Only finished meetings
    are eligible — a `failed` one might still be retried, so its recording stays.
    `recording_deleted` makes this idempotent, and the delete lands in Zoom's own
    trash (~30 days recoverable) rather than being destroyed outright.
    """
    settings = get_settings()
    days = settings.zoom_recording_retention_days
    if days <= 0:
        return {"status": "disabled"}
    if not (settings.zoom_account_id and settings.zoom_client_id and settings.zoom_client_secret):
        return {"status": "no_credentials"}

    cutoff = datetime.utcnow() - timedelta(days=days)
    db = SessionLocal()
    deleted = failed = 0
    try:
        stale = (
            db.query(ZoomMeeting)
            .filter(
                ZoomMeeting.recording_deleted.is_(False),
                ZoomMeeting.status.in_((ZoomMeetingStatus.completed, ZoomMeetingStatus.skipped)),
                ZoomMeeting.completed_at.isnot(None),
                ZoomMeeting.completed_at < cutoff,
            )
            .all()
        )
        if not stale:
            return {"status": "ok", "deleted": 0}

        from ..services.zoom_client import ZoomClient

        with ZoomClient() as zoom:
            for zm in stale:
                try:
                    zoom.delete_recording(zm.zoom_meeting_uuid)
                    zm.recording_deleted = True
                    db.commit()
                    deleted += 1
                except Exception as exc:  # noqa: BLE001 — one bad delete mustn't stop the sweep
                    db.rollback()
                    failed += 1
                    log.warning(
                        "recording cleanup failed for one meeting",
                        extra={"meeting": zm.zoom_meeting_uuid, "error": str(exc)[:200]},
                    )
        log.info("zoom recording cleanup done", extra={"deleted": deleted, "failed": failed, "retention_days": days})
        return {"status": "ok", "deleted": deleted, "failed": failed}
    finally:
        db.close()
