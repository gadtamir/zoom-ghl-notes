"""Admin endpoints — a token-protected window into the Zoom pipeline.

The whole reason this exists: when the pipeline misbehaves, there was no way to
see the server's state from outside (the DB is IP-walled, one-off jobs don't run,
there's no SSH key), so incidents stayed invisible for days. These endpoints give
a reliable read into `zoom_meetings` plus one manual recovery lever.

Enabled only when `ADMIN_API_TOKEN` is set — otherwise every call is 503, so the
admin surface is never reachable unprotected. The token is compared in constant
time. Reads report status only (no transcripts); the one write re-runs the poller,
which is idempotent (note creation dedupes), so triggering it is always safe.
"""
import logging
import secrets

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import JSONResponse
from sqlalchemy import func

from ..config import get_settings
from ..db import SessionLocal
from ..models import CallJob, CallJobStatus, ZoomMeeting, ZoomMeetingStatus

log = logging.getLogger(__name__)
router = APIRouter(prefix="/admin", tags=["admin"])


def _auth(token: str | None) -> None:
    expected = get_settings().admin_api_token
    if not expected:
        raise HTTPException(status_code=503, detail="ADMIN_API_TOKEN not configured")
    if not token or not secrets.compare_digest(token, expected):
        raise HTTPException(status_code=401, detail="bad token")


def _status_value(s) -> str:
    return s.value if hasattr(s, "value") else str(s)


@router.get("/zoom/summary")
def zoom_summary(token: str | None = Query(default=None)) -> JSONResponse:
    """Count of zoom meetings by status — a one-glance health check."""
    _auth(token)
    db = SessionLocal()
    try:
        counts = {_status_value(s): n for s, n in
                  db.query(ZoomMeeting.status, func.count()).group_by(ZoomMeeting.status).all()}
        return JSONResponse({s.value: counts.get(s.value, 0) for s in ZoomMeetingStatus})
    finally:
        db.close()


@router.get("/zoom/meetings")
def zoom_meetings(token: str | None = Query(default=None), limit: int = 50) -> JSONResponse:
    """Recent zoom meetings with status + error — the view that was missing during
    the incident. No transcripts, just the pipeline state per meeting."""
    _auth(token)
    db = SessionLocal()
    try:
        rows = (db.query(ZoomMeeting).order_by(ZoomMeeting.created_at.desc())
                .limit(max(1, min(limit, 200))).all())
        return JSONResponse([{
            "uuid": r.zoom_meeting_uuid,
            "topic": r.topic,
            "started_at": r.started_at.isoformat() if r.started_at else None,
            "status": _status_value(r.status),
            "attempts": r.attempts,
            "error": r.error_message,
            "contact_id": r.ghl_contact_id,
            "note_id": r.ghl_note_id,
        } for r in rows])
    finally:
        db.close()


@router.get("/zoom/skipped")
def zoom_skipped(token: str | None = Query(default=None), limit: int = 100) -> JSONResponse:
    """Meetings the title filter declined to transcribe — the answer to "what did
    we not process, and should we have?".

    `near_miss` flags a title that names some meeting type we don't recognise;
    those are the ones worth reading, because they're how a renamed or newly
    introduced meeting type shows up. Also returns the configured type list, so
    the filter's behaviour can be checked against it without reading the code.
    """
    _auth(token)
    from ..tasks.zoom_meetings import _LOOKS_LIKE_A_MEETING, _configured_meeting_types, _normalise

    db = SessionLocal()
    try:
        rows = (
            db.query(ZoomMeeting)
            .filter(ZoomMeeting.status == ZoomMeetingStatus.skipped,
                    ZoomMeeting.error_message.like("meeting type not in transcribe list%"))
            .order_by(ZoomMeeting.created_at.desc())
            .limit(max(1, min(limit, 500)))
            .all()
        )
        return JSONResponse({
            "configured_types": _configured_meeting_types(),
            "count": len(rows),
            "skipped_minutes": sum(r.duration_minutes or 0 for r in rows),
            "meetings": [{
                "topic": r.topic,
                "minutes": r.duration_minutes,
                "started_at": r.started_at.isoformat() if r.started_at else None,
                "host": r.host_email,
                "near_miss": bool(_LOOKS_LIKE_A_MEETING.search(_normalise(r.topic))),
            } for r in rows],
        })
    finally:
        db.close()


@router.post("/zoom/recover")
def zoom_recover(token: str | None = Query(default=None)) -> JSONResponse:
    """Run the poller now instead of waiting for the next 15-min tick. It re-enqueues
    every non-terminal recording and recovers no-audio-stuck rows; idempotent note
    creation means firing this can never double-post."""
    _auth(token)
    from ..tasks.zoom_meetings import poll_recordings
    res = poll_recordings.delay()
    log.info("admin triggered zoom poll", extra={"task_id": res.id})
    return JSONResponse({"triggered": True, "task_id": str(res.id)})


# ---- phone calls -----------------------------------------------------------
# Same idea for the GHL phone-call pipeline: see which calls the reconciler gave
# up on (`abandoned` — the ones the one-time alert is about), and re-run one
# after the root cause is fixed (typically: API credit reloaded).

_CALL_PROBLEM_STATUSES = (CallJobStatus.abandoned, CallJobStatus.failed)


def _call_row(r: CallJob) -> dict:
    return {
        "id": r.id,
        "message_id": r.ghl_message_id,
        "contact_id": r.ghl_contact_id,
        "owner": r.ghl_user_name,
        "direction": r.direction,
        "duration_seconds": r.duration_seconds,
        "call_started_at": r.call_started_at.isoformat() if r.call_started_at else None,
        "status": _status_value(r.status),
        "attempts": r.attempts,
        "error": r.error_message,
        "note_id": r.ghl_note_id,
    }


@router.get("/calls/summary")
def calls_summary(token: str | None = Query(default=None)) -> JSONResponse:
    """Count of phone-call jobs by status."""
    _auth(token)
    db = SessionLocal()
    try:
        counts = {_status_value(s): n for s, n in
                  db.query(CallJob.status, func.count()).group_by(CallJob.status).all()}
        return JSONResponse({s.value: counts.get(s.value, 0) for s in CallJobStatus})
    finally:
        db.close()


@router.get("/calls/failed")
def calls_failed(token: str | None = Query(default=None), limit: int = 50) -> JSONResponse:
    """Calls that are `abandoned` (reconciler gave up, admin was alerted once) or
    still `failed` (awaiting the reconciler). Newest first, no transcripts."""
    _auth(token)
    db = SessionLocal()
    try:
        rows = (db.query(CallJob).filter(CallJob.status.in_(_CALL_PROBLEM_STATUSES))
                .order_by(CallJob.created_at.desc()).limit(max(1, min(limit, 200))).all())
        return JSONResponse([_call_row(r) for r in rows])
    finally:
        db.close()


@router.post("/calls/retry")
def calls_retry(token: str | None = Query(default=None), id: str = Query(...)) -> JSONResponse:
    """Re-run one call from scratch with a fresh attempt budget. Refuses calls
    that already produced a note, so this can never double-post."""
    _auth(token)
    from ..tasks.phone_calls import process_call_job
    db = SessionLocal()
    try:
        cj = db.query(CallJob).filter(CallJob.id == id).first()
        if not cj:
            raise HTTPException(status_code=404, detail="call job not found")
        if cj.status == CallJobStatus.completed or cj.ghl_note_id:
            raise HTTPException(status_code=409, detail="already completed — refusing to re-run")
        cj.status = CallJobStatus.received
        cj.error_message = None
        cj.completed_at = None
        cj.attempts = 0
        db.commit()
    finally:
        db.close()
    res = process_call_job.delay(id)
    log.info("admin triggered call retry", extra={"call_job_id": id, "task_id": res.id})
    return JSONResponse({"triggered": True, "call_job_id": id, "task_id": str(res.id)})
