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
import json
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


# --- closed deals → Drive transcript doc ---------------------------------------

def _deal_row(d) -> dict:
    return {
        "id": d.id,
        "contact_id": d.ghl_contact_id,
        "name": d.contact_name,
        "outcome": d.outcome,
        "reason": d.reason,
        "closed_at": d.closed_at.isoformat() if d.closed_at else None,
        "status": _status_value(d.status),
        "doc": d.gdoc_link,
        "meetings": len(json.loads(d.included_keys or "[]")),
        "error": d.error_message,
    }


@router.get("/deals")
def deals_list(token: str | None = Query(default=None), limit: int = 50) -> JSONResponse:
    """Won/lost leads and their transcript docs, newest first."""
    _auth(token)
    from ..models import ClosedDeal
    db = SessionLocal()
    try:
        rows = (db.query(ClosedDeal).order_by(ClosedDeal.closed_at.desc())
                .limit(max(1, min(limit, 200))).all())
        return JSONResponse([_deal_row(d) for d in rows])
    finally:
        db.close()


@router.get("/deals/preview")
def deals_preview(token: str | None = Query(default=None), contact_id: str = Query(...)) -> JSONResponse:
    """Which sales meetings would go into this contact's doc — read-only."""
    _auth(token)
    from ..tasks.closed_deals import collect_meetings
    db = SessionLocal()
    try:
        return JSONResponse([
            {"key": m.key, "type": m.kind,
             "started_at": m.started_at.isoformat() if m.started_at else None,
             "has_transcript": bool(m.transcript), "in_progress": m.in_progress}
            for m in collect_meetings(db, contact_id)
        ])
    finally:
        db.close()


@router.post("/deals/run")
def deals_run(token: str | None = Query(default=None), contact_id: str = Query(...),
              name: str | None = Query(default=None),
              outcome: str = Query(default="won", pattern="^(won|lost)$"),
              opportunity_id: str | None = Query(default=None)) -> JSONResponse:
    """Build (or refresh) a contact's transcript doc now, without waiting — for
    leads that were moved before this existed, or to test the setup. Pass
    `opportunity_id` to include the opportunity's reason field."""
    _auth(token)
    from ..tasks.closed_deals import process_deal, register_deal
    db = SessionLocal()
    try:
        deal, _ = register_deal(db, contact_id, name, opportunity_id, outcome=outcome)
        result = process_deal(db, deal, force=True)
        return JSONResponse({"result": result, **_deal_row(deal)})
    finally:
        db.close()


# Backfill: a page a human can read — who would get a doc, and in which folder —
# with a button that actually builds them.

_BACKFILL_PAGE = """<!doctype html><html lang="he" dir="rtl"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>לידים לתיקיות התמלולים</title><style>
body{{font-family:system-ui,Arial,sans-serif;margin:24px;color:#1a1a1a;background:#fff}}
table{{border-collapse:collapse;width:100%;margin:12px 0}}
th,td{{border-bottom:1px solid #ddd;padding:8px 10px;text-align:right;vertical-align:top}}
th{{background:#f5f5f5}} .muted{{color:#777}} .warn{{color:#b45309}}
button{{font-size:16px;padding:10px 18px;cursor:pointer}}
</style></head><body>{body}</body></html>"""


@router.get("/deals/backfill")
def deals_backfill_preview(token: str | None = Query(default=None), days: int = Query(default=14, ge=1, le=120)):
    """Read-only preview: leads moved to won/lost in the last `days` days that have
    a sales meeting on record, and the folder each doc would go to."""
    _auth(token)
    import html as _html
    from datetime import datetime, timedelta

    from fastapi.responses import HTMLResponse

    from ..models import ClosedDeal
    from ..tasks.closed_deals import OUTCOMES, _il, _meeting_heading, find_candidates

    folder = {"won": "נסגרו", "lost": "לא נסגרו"}
    db = SessionLocal()
    try:
        candidates, truncated = find_candidates(db, datetime.utcnow() - timedelta(days=days))
        rows = []
        for c in candidates:
            existing = (db.query(ClosedDeal)
                        .filter(ClosedDeal.ghl_opportunity_id == c.opportunity_id, ClosedDeal.outcome == c.outcome)
                        .first()) if c.opportunity_id else None
            meetings = "<br>".join(
                _html.escape(_meeting_heading(m)) + ("" if m.transcript else ' <span class="warn">(עדיין בתמלול)</span>')
                for m in c.meetings
            )
            state = "דוק קיים" if existing and existing.gdoc_id else "ייווצר דוק"
            rows.append(
                f"<tr><td>{_html.escape(c.name or c.contact_id)}</td>"
                f"<td>{folder[c.outcome]}</td><td>{_il(c.changed_at):%d/%m/%Y}</td>"
                f"<td>{meetings}</td><td>{state}</td></tr>"
            )
        won = sum(c.outcome == "won" for c in candidates)
        body = (
            f"<h2>לידים שעברו שלב ב-{days} הימים האחרונים ויש להם פגישת מכירה</h2>"
            f"<p>{won} ל\"נסגרו\" · {len(candidates) - won} ל\"לא נסגרו\". "
            f"שלבים: {_html.escape(OUTCOMES['won'].stage_name)} / {_html.escape(OUTCOMES['lost'].stage_name)}.</p>"
            + ('<p class="warn">השלב גדול מדי ולא נסרק עד הסוף, ייתכן שחסרים לידים.</p>' if truncated else "")
            + "<table><tr><th>ליד</th><th>תיקייה</th><th>עבר שלב</th><th>פגישות שייכנסו</th><th>מצב</th></tr>"
            + ("".join(rows) or '<tr><td colspan="5" class="muted">לא נמצאו לידים מתאימים</td></tr>')
            + "</table>"
            + (f'<form method="post" action="/admin/deals/backfill?token={_html.escape(token or "")}&days={days}">'
               f"<button>צור את הדוקים ({len(candidates)})</button></form>"
               '<p class="muted">הלחיצה יוצרת את הדוקים בדרייב. לידים שכבר יש להם דוק לא יקבלו דוק נוסף.</p>'
               if candidates else "")
        )
        return HTMLResponse(_BACKFILL_PAGE.format(body=body))
    finally:
        db.close()


@router.post("/deals/backfill")
def deals_backfill_run(token: str | None = Query(default=None), days: int = Query(default=14, ge=1, le=120)):
    """Build the docs for the leads the preview lists. Runs in the background."""
    _auth(token)
    from fastapi.responses import HTMLResponse

    from ..tasks.closed_deals import backfill_closed_deals

    res = backfill_closed_deals.delay(days)
    log.info("admin triggered lead docs backfill", extra={"days": days, "task_id": res.id})
    return HTMLResponse(_BACKFILL_PAGE.format(body=(
        "<h2>התחלתי ליצור את הדוקים</h2>"
        "<p>זה רץ ברקע ולוקח כמה דקות. הדוקים יופיעו בתיקיות בדרייב.</p>"
    )))
