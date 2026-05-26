"""Phone-call pipeline: discover, transcribe, summarize, attach note.

Two tasks:
  - poll_ghl_calls (run by Celery beat every 3h):
      Walks recent GHL conversations, finds new TYPE_CALL messages over 1 min,
      creates a CallJob row for each, dispatches process_call_job.

  - process_call_job (per-call worker task):
      Downloads the WAV from GHL, transcribes via OpenAI (chunking-safe via
      ffmpeg segment muxer if needed), summarizes via Claude using the phone-call
      prompt, creates a note on the contact in GHL.
"""

import logging
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy.orm import Session

from ..config import get_settings
from ..db import SessionLocal
from ..models import CallJob, CallJobStatus
from ..services.anthropic_client import summarize_phone_call
from ..services.ghl_client import GHLClient, GHLError
from .celery_app import celery_app
from .transcribe import transcribe_audio


log = logging.getLogger(__name__)


MIN_DURATION_SEC = 30
DEFAULT_POLL_WINDOW_HOURS = 12     # generous overlap window — dedup via ghl_message_id
MAX_CONVS_PER_POLL = 200           # cap to avoid runaway scans on a large workspace

# GHL emits at least two phone-call message types. TYPE_CALL is a manual call;
# TYPE_CAMPAIGN_CALL is a call placed via a campaign / dialer (the path most
# of Vika's scheduled callbacks go through). Both have a downloadable recording
# on the same endpoint, so we treat them identically.
CALL_MESSAGE_TYPES = {"TYPE_CALL", "TYPE_CAMPAIGN_CALL"}


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    raw = value.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(raw).replace(tzinfo=None)
    except ValueError:
        return None


def _call_storage_path(call_job_id: str) -> Path:
    settings = get_settings()
    return Path(settings.upload_dir) / f"call-{call_job_id}.wav"


def _format_note(cj: CallJob) -> str:
    when = cj.call_started_at.strftime("%Y-%m-%d %H:%M") if cj.call_started_at else "?"
    duration = f"{cj.duration_seconds // 60} דק' {cj.duration_seconds % 60} שניות"
    title_owner = cj.ghl_user_name or "—"
    direction_he = {"inbound": "נכנסת", "outbound": "יוצאת"}.get(cj.direction or "", cj.direction or "")
    title = f"☎️ סיכום שיחה {direction_he} - {when} ({duration}) - {title_owner}"
    body = cj.summary or "(אין סיכום זמין)"
    return f"{title}\n\n{body}"


@celery_app.task(name="phone_calls.poll")
def poll_ghl_calls(hours_back: int = DEFAULT_POLL_WINDOW_HOURS) -> dict:
    """Discover new TYPE_CALL messages, enqueue per-call processing.

    Scans conversations whose lastMessage is within the last `hours_back` hours.
    Default 12h is right for the scheduled run; admins can pass a larger value
    via the CLI to backfill historical calls.
    """
    settings = get_settings()
    since = datetime.utcnow() - timedelta(hours=hours_back)
    since_ms = int(since.timestamp() * 1000)
    db = SessionLocal()
    new_calls = 0
    skipped_dup = 0
    skipped_short = 0
    scanned_convs = 0
    try:
        with GHLClient() as ghl:
            start_after = None
            while scanned_convs < MAX_CONVS_PER_POLL:
                convs = ghl.search_conversations(limit=25, start_after_date=start_after)
                if not convs:
                    break
                last_updated = None
                for c in convs:
                    scanned_convs += 1
                    last_updated = c.get("dateUpdated")
                    msgs = ghl.list_messages(c["id"], limit=100)
                    for m in msgs:
                        if m.get("messageType") not in CALL_MESSAGE_TYPES:
                            continue
                        msg_added = _parse_iso(m.get("dateAdded"))
                        if msg_added and msg_added < since:
                            continue
                        message_id = m.get("id")
                        if not message_id:
                            continue
                        duration = (m.get("meta") or {}).get("call", {}).get("duration")
                        # GHL sometimes returns duration=None even when a real
                        # recording exists (metadata bug). Skip only when we
                        # have a confirmed-short duration; if it's missing,
                        # let the download step decide via the 422 path.
                        if duration is not None and duration < MIN_DURATION_SEC:
                            skipped_short += 1
                            continue
                        if db.query(CallJob).filter(CallJob.ghl_message_id == message_id).first():
                            skipped_dup += 1
                            continue
                        cj = CallJob(
                            ghl_message_id=message_id,
                            ghl_conversation_id=c["id"],
                            ghl_contact_id=m.get("contactId") or "",
                            ghl_user_id=m.get("userId"),
                            direction=m.get("direction"),
                            duration_seconds=duration or 0,
                            from_number=m.get("from"),
                            to_number=m.get("to"),
                            call_started_at=msg_added,
                            status=CallJobStatus.received,
                        )
                        db.add(cj)
                        db.commit()
                        db.refresh(cj)
                        new_calls += 1
                        log.info(
                            "call_job created",
                            extra={"call_job_id": cj.id, "message_id": message_id, "duration": duration, "contact": cj.ghl_contact_id},
                        )
                        process_call_job.delay(cj.id)
                if last_updated is None or last_updated < since_ms:
                    break
                start_after = last_updated
    finally:
        db.close()
    summary = {
        "since": since.isoformat(),
        "scanned_conversations": scanned_convs,
        "new_calls": new_calls,
        "skipped_dup": skipped_dup,
        "skipped_short": skipped_short,
    }
    log.info("poll_ghl_calls done", extra=summary)
    return summary


@celery_app.task(name="phone_calls.poll_pipeline", time_limit=60 * 60)
def poll_pipeline_calls(
    pipeline_id: str,
    exclude_stage_ids: list[str] | None = None,
    owner_user_id: str | None = None,
) -> dict:
    """Backfill: for every contact attached to an opportunity in the given pipeline
    (excluding the listed stages), discover their TYPE_CALL messages > 1 min and
    enqueue per-call processing. Dedupe via the existing ghl_message_id constraint.
    """
    exclude_set = set(exclude_stage_ids or [])
    db = SessionLocal()
    seen_contacts: set[str] = set()
    new_calls = 0
    skipped_dup = 0
    skipped_short = 0
    opportunities_scanned = 0
    excluded_opps = 0
    try:
        with GHLClient() as ghl:
            start_after: int | None = None
            start_after_id: str | None = None
            while True:
                page = ghl.opportunities_page(
                    pipeline_id=pipeline_id,
                    limit=100,
                    start_after=start_after,
                    start_after_id=start_after_id,
                )
                opps = page.get("opportunities", [])
                if not opps:
                    break
                for o in opps:
                    opportunities_scanned += 1
                    stage_id = o.get("pipelineStageId", "")
                    if stage_id in exclude_set:
                        excluded_opps += 1
                        continue
                    contact_id = o.get("contactId")
                    if not contact_id or contact_id in seen_contacts:
                        continue
                    seen_contacts.add(contact_id)

                    convs = ghl.search_conversations(limit=25, contact_id=contact_id)
                    for c in convs:
                        msgs = ghl.list_messages(c["id"], limit=100)
                        for m in msgs:
                            if m.get("messageType") not in CALL_MESSAGE_TYPES:
                                continue
                            if owner_user_id and m.get("userId") != owner_user_id:
                                continue
                            duration = (m.get("meta") or {}).get("call", {}).get("duration")
                            # See poll_ghl_calls — duration may be None despite a real recording.
                            if duration is not None and duration < MIN_DURATION_SEC:
                                skipped_short += 1
                                continue
                            message_id = m.get("id")
                            if not message_id:
                                continue
                            if db.query(CallJob).filter(CallJob.ghl_message_id == message_id).first():
                                skipped_dup += 1
                                continue
                            cj = CallJob(
                                ghl_message_id=message_id,
                                ghl_conversation_id=c["id"],
                                ghl_contact_id=contact_id,
                                ghl_user_id=m.get("userId"),
                                direction=m.get("direction"),
                                duration_seconds=duration or 0,
                                from_number=m.get("from"),
                                to_number=m.get("to"),
                                call_started_at=_parse_iso(m.get("dateAdded")),
                                status=CallJobStatus.received,
                            )
                            db.add(cj)
                            db.commit()
                            db.refresh(cj)
                            new_calls += 1
                            log.info(
                                "call_job (pipeline) created",
                                extra={"call_job_id": cj.id, "message_id": message_id, "duration": duration, "contact": contact_id},
                            )
                            process_call_job.delay(cj.id)

                meta = page.get("meta", {})
                next_page = meta.get("nextPage")
                if not next_page:
                    break
                start_after = meta.get("startAfter")
                start_after_id = meta.get("startAfterId")
                if not start_after or not start_after_id:
                    break
    finally:
        db.close()

    summary = {
        "pipeline_id": pipeline_id,
        "opportunities_scanned": opportunities_scanned,
        "excluded_opportunities": excluded_opps,
        "unique_contacts": len(seen_contacts),
        "new_calls": new_calls,
        "skipped_dup": skipped_dup,
        "skipped_short": skipped_short,
    }
    log.info("poll_pipeline_calls done", extra=summary)
    return summary


@celery_app.task(name="phone_calls.process", bind=True, max_retries=0)
def process_call_job(self, call_job_id: str) -> dict:
    db = SessionLocal()
    audio_path: Path | None = None
    try:
        cj = db.query(CallJob).filter(CallJob.id == call_job_id).first()
        if not cj:
            return {"call_job_id": call_job_id, "status": "not_found"}
        cj.attempts += 1
        db.commit()

        try:
            with GHLClient() as ghl:
                audio = ghl.download_call_recording(cj.ghl_message_id)
                # User name is best-effort metadata; a 400/404 here must not kill the job.
                if cj.ghl_user_id and not cj.ghl_user_name:
                    try:
                        user = ghl.get_user(cj.ghl_user_id)
                        if user:
                            cj.ghl_user_name = user.get("name") or " ".join(
                                filter(None, [user.get("firstName"), user.get("lastName")])
                            )
                    except GHLError as exc:
                        log.warning("get_user failed — continuing without name", extra={"call_job_id": cj.id, "user_id": cj.ghl_user_id, "err": str(exc)[:200]})
        except GHLError as exc:
            # 422 commonly means: call exists but has no recording (didn't connect,
            # voicemail without record, externally-imported call, etc.). Mark
            # as `skipped` so it doesn't show up as a real failure.
            msg = str(exc)
            if "422" in msg:
                log.info("call has no recording — skipping", extra={"call_job_id": cj.id, "ghl_msg": cj.ghl_message_id})
                cj.status = CallJobStatus.skipped
                cj.error_message = "no recording (HTTP 422)"
                cj.completed_at = datetime.utcnow()
                db.commit()
                return {"call_job_id": cj.id, "status": "skipped", "reason": "no recording"}
            log.exception("download failed", extra={"call_job_id": cj.id})
            return _fail(db, cj, f"download: {exc}")
        except Exception as exc:
            log.exception("download failed", extra={"call_job_id": cj.id})
            return _fail(db, cj, f"download: {exc}")

        audio_path = _call_storage_path(cj.id)
        audio_path.parent.mkdir(parents=True, exist_ok=True)
        audio_path.write_bytes(audio)
        cj.status = CallJobStatus.downloaded
        db.commit()
        log.info("recording saved", extra={"call_job_id": cj.id, "bytes": len(audio), "path": str(audio_path)})

        try:
            cj.transcript = transcribe_audio(audio_path, language="he")
            cj.status = CallJobStatus.transcribed
            db.commit()
        except Exception as exc:
            log.exception("transcribe failed", extra={"call_job_id": cj.id})
            return _fail(db, cj, f"transcribe: {exc}")

        try:
            cj.summary = summarize_phone_call(
                transcript=cj.transcript or "",
                employee_name=cj.ghl_user_name or "(לא ידוע)",
                duration_seconds=cj.duration_seconds,
            )
            cj.status = CallJobStatus.summarized
            db.commit()
        except Exception as exc:
            log.exception("summarize failed", extra={"call_job_id": cj.id})
            return _fail(db, cj, f"summarize: {exc}")

        try:
            with GHLClient() as ghl:
                note = ghl.create_note(contact_id=cj.ghl_contact_id, body=_format_note(cj))
            cj.ghl_note_id = note.get("id")
            cj.status = CallJobStatus.completed
            cj.completed_at = datetime.utcnow()
            db.commit()
        except Exception as exc:
            log.exception("create_note failed", extra={"call_job_id": cj.id})
            return _fail(db, cj, f"create_note: {exc}")

        log.info("call_job completed", extra={"call_job_id": cj.id, "contact": cj.ghl_contact_id, "note": cj.ghl_note_id})
        return {"call_job_id": cj.id, "status": "completed", "note_id": cj.ghl_note_id}
    finally:
        if audio_path and audio_path.exists():
            try:
                audio_path.unlink()
            except OSError:
                pass
        db.close()


def _fail(db: Session, cj: CallJob, msg: str) -> dict:
    cj.status = CallJobStatus.failed
    cj.error_message = msg
    cj.completed_at = datetime.utcnow()
    db.commit()
    return {"call_job_id": cj.id, "status": "failed", "error": msg}
