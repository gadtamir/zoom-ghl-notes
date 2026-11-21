"""Closed deals → one Google Doc of sales-meeting transcripts per client.

Flow:
  1. `closed_deals.check` polls GHL for opportunities that moved into "ליד נסגר"
     (pipeline "ליד ראשי") within the lookback and records a `ClosedDeal` for each.
     (A GHL workflow can also POST to /webhooks/ghl/deal-closed — optional.)
  2. The same task (Celery beat, every 15 min) walks the pending deals.
     Nothing is written for the first hour after closing — the deal is often closed
     on the call that is still being transcribed. After that the doc is created as
     soon as no matching meeting is still mid-pipeline, and rewritten whenever a new
     transcript shows up, until the window (24h) ends.

Sources: Zoom cloud recordings (`ZoomMeeting`) and desktop-uploader jobs (`Job`),
both already matched to a GHL contact by the transcription pipeline. Only titles
naming a configured sales-meeting type are included.
"""

import html
import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from ..config import get_settings
from ..db import SessionLocal
from ..models import (
    ClosedDeal,
    ClosedDealStatus,
    Job,
    JobStatus,
    ZoomMeeting,
    ZoomMeetingStatus,
)
from .celery_app import celery_app


log = logging.getLogger(__name__)

_IL = ZoneInfo("Asia/Jerusalem")

# A webhook fired twice for the same close (workflow re-entry, manual re-run) must
# not produce a second doc — reuse a deal for the same contact this recent.
_DEDUPE_WINDOW = timedelta(days=7)

# Zoom meetings in these states will never get a transcript, so they don't hold
# the doc back.
_ZOOM_DEAD = {ZoomMeetingStatus.skipped, ZoomMeetingStatus.failed}
_JOB_DEAD = {JobStatus.failed, JobStatus.unmatched}


def _meeting_types() -> list[str]:
    raw = get_settings().closed_deals_meeting_types or ""
    # Longest first so "פגישת סיכום והתחלת עבודה" is reported over a shorter overlap.
    return sorted((t.strip() for t in raw.split(",") if t.strip()), key=len, reverse=True)


def sales_meeting_type(topic: str | None) -> str | None:
    title = re.sub(r"\s+", " ", topic or "").strip()
    for t in _meeting_types():
        if t in title:
            return t
    return None


@dataclass
class _Meeting:
    key: str
    kind: str                # the matched meeting type
    started_at: datetime | None   # Israel local, naive
    duration_minutes: int | None
    transcript: str | None
    in_progress: bool


def _il(dt_utc: datetime | None) -> datetime | None:
    if dt_utc is None:
        return None
    return dt_utc.replace(tzinfo=timezone.utc).astimezone(_IL).replace(tzinfo=None)


def _job_date(job: Job) -> datetime | None:
    if job.meeting_date:
        try:
            return datetime.strptime(job.meeting_date[:10], "%Y-%m-%d")
        except ValueError:
            pass
    return _il(job.created_at)


def collect_meetings(db: Session, contact_id: str) -> list[_Meeting]:
    """All sales meetings on record for this contact, oldest first."""
    out: list[_Meeting] = []
    for zm in db.query(ZoomMeeting).filter(ZoomMeeting.ghl_contact_id == contact_id).all():
        kind = sales_meeting_type(zm.topic)
        if not kind or zm.status in _ZOOM_DEAD and not zm.transcript:
            continue
        out.append(_Meeting(
            key=f"zoom:{zm.id}", kind=kind, started_at=_il(zm.started_at),
            duration_minutes=zm.duration_minutes or None, transcript=zm.transcript,
            in_progress=not zm.transcript,
        ))
    for job in db.query(Job).filter(Job.ghl_contact_id == contact_id).all():
        kind = sales_meeting_type(job.meeting_topic or job.original_filename)
        if not kind or job.status in _JOB_DEAD and not job.transcript:
            continue
        out.append(_Meeting(
            key=f"job:{job.id}", kind=kind, started_at=_job_date(job),
            duration_minutes=None, transcript=job.transcript,
            in_progress=not job.transcript,
        ))
    out.sort(key=lambda m: m.started_at or datetime.min)
    return out


def _paragraphs(text: str) -> str:
    parts = [p.strip() for p in re.split(r"\n\s*\n|\n", text) if p.strip()]
    return "\n".join(f'<p dir="rtl" style="text-align:right">{html.escape(p)}</p>' for p in parts)


def render_doc(name: str, closed_at_il: datetime, meetings: list[_Meeting]) -> str:
    blocks = [
        f'<h1 dir="rtl" style="text-align:right">{html.escape(name)} — תמלולי פגישות מכירה</h1>',
        f'<p dir="rtl" style="text-align:right">נסגר בתאריך {closed_at_il:%d/%m/%Y} · '
        f'{len(meetings)} פגישות</p>',
    ]
    for m in meetings:
        when = f"{m.started_at:%d/%m/%Y %H:%M}" if m.started_at else "תאריך לא ידוע"
        if m.key.startswith("job:") and m.started_at:
            when = f"{m.started_at:%d/%m/%Y}"
        dur = f" · {m.duration_minutes} דק'" if m.duration_minutes else ""
        blocks.append(f'<h2 dir="rtl" style="text-align:right">{html.escape(m.kind)} — {when}{dur}</h2>')
        blocks.append(_paragraphs(m.transcript or ""))
    return (
        '<html><head><meta charset="utf-8"></head><body dir="rtl">'
        + "\n".join(blocks)
        + "</body></html>"
    )


def register_deal(
    db: Session,
    contact_id: str,
    name: str | None,
    opportunity_id: str | None,
    closed_at: datetime | None = None,
) -> tuple[ClosedDeal, bool]:
    """Record a closed deal. Returns (deal, created). Idempotent per contact within
    the dedupe window."""
    since = datetime.utcnow() - _DEDUPE_WINDOW
    existing = (
        db.query(ClosedDeal)
        .filter(ClosedDeal.ghl_contact_id == contact_id, ClosedDeal.closed_at >= since)
        .order_by(ClosedDeal.closed_at.desc())
        .first()
    )
    if existing:
        return existing, False
    deal = ClosedDeal(ghl_contact_id=contact_id, contact_name=name, ghl_opportunity_id=opportunity_id)
    if closed_at:
        deal.closed_at = closed_at
    db.add(deal)
    db.commit()
    db.refresh(deal)
    log.info("closed deal registered", extra={"deal": deal.id, "contact": contact_id})
    return deal, True


def _resolve_name(deal: ClosedDeal) -> str:
    if deal.contact_name:
        return deal.contact_name
    try:
        from ..services.ghl_client import GHLClient

        with GHLClient() as ghl:
            c = ghl.get_contact(deal.ghl_contact_id) or {}
        name = (c.get("contactName") or c.get("name")
                or " ".join(x for x in (c.get("firstName"), c.get("lastName")) if x)).strip()
        if name:
            deal.contact_name = name
            return name
    except Exception as exc:  # name is cosmetic — never block the doc on it
        log.warning("closed deal: contact name lookup failed", extra={"deal": deal.id, "error": str(exc)})
    return deal.ghl_contact_id


def process_deal(db: Session, deal: ClosedDeal, now: datetime | None = None, force: bool = False) -> str:
    """Advance one pending deal. Returns a short outcome string for logs/admin.

    `force` skips the initial wait and the in-progress hold (manual runs)."""
    s = get_settings()
    now = now or datetime.utcnow()
    first_write_at = deal.closed_at + timedelta(minutes=s.closed_deals_initial_wait_minutes)
    deadline = deal.closed_at + timedelta(hours=s.closed_deals_window_hours)

    if not force and now < first_write_at:
        return "waiting_initial"

    meetings = collect_meetings(db, deal.ghl_contact_id)
    ready = [m for m in meetings if m.transcript]
    busy = [m for m in meetings if m.in_progress]
    included = set(json.loads(deal.included_keys or "[]"))
    ready_keys = {m.key for m in ready}

    outcome = "unchanged"
    hold = busy and now < deadline and not force
    if ready and ready_keys != included and not hold:
        name = _resolve_name(deal)
        doc_html = render_doc(name, _il(deal.closed_at), ready)
        title = f"{name} - תמלולי מכירה - {_il(deal.closed_at):%d.%m.%Y}"
        from ..services.gdrive_client import upsert_html_gdoc

        f = upsert_html_gdoc(title, doc_html, s.closed_deals_folder_id, file_id=deal.gdoc_id)
        deal.gdoc_id = f["id"]
        deal.gdoc_link = f.get("webViewLink") or deal.gdoc_link
        deal.included_keys = json.dumps(sorted(ready_keys))
        outcome = "doc_updated" if included else "doc_created"
        log.info("closed deal doc written", extra={
            "deal": deal.id, "meetings": len(ready), "doc": deal.gdoc_id, "outcome": outcome,
        })
    elif hold:
        outcome = "waiting_transcripts"

    if now >= deadline and not busy or now >= deadline + timedelta(hours=s.closed_deals_window_hours):
        # Past the window (or far past it, if a meeting is stuck mid-pipeline).
        deal.status = ClosedDealStatus.done if deal.gdoc_id else ClosedDealStatus.no_meetings
        deal.completed_at = now
        outcome = f"{outcome}+{deal.status.value}"

    deal.error_message = None
    deal.updated_at = now
    db.commit()
    return outcome


def _parse_ghl_time(value) -> datetime | None:
    """GHL timestamps → naive UTC. Accepts ISO strings or epoch millis."""
    if not value:
        return None
    try:
        if isinstance(value, (int, float)):
            return datetime.utcfromtimestamp(value / 1000)
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return dt.astimezone(timezone.utc).replace(tzinfo=None) if dt.tzinfo else dt
    except (ValueError, OverflowError, OSError):
        return None


def _resolve_stage(ghl) -> tuple[str, str] | None:
    """(pipeline_id, stage_id) for the configured closed stage, by name."""
    s = get_settings()
    want_p = s.closed_deals_pipeline_name.strip().lower()
    want_s = s.closed_deals_stage_name.strip().lower()
    for p in ghl.list_pipelines():
        if want_p not in (p.get("name") or "").lower():
            continue
        for st in p.get("stages", []):
            if want_s in (st.get("name") or "").lower():
                return p["id"], st["id"]
    log.error("closed deals: pipeline/stage not found", extra={
        "pipeline": s.closed_deals_pipeline_name, "stage": s.closed_deals_stage_name,
    })
    return None


def discover_closed_deals(db: Session, now: datetime | None = None) -> int:
    """Register every opportunity that moved into the closed stage within the
    lookback window. Returns how many new deals were registered."""
    from ..services.ghl_client import GHLClient

    s = get_settings()
    now = now or datetime.utcnow()
    since = now - timedelta(hours=s.closed_deals_poll_lookback_hours)
    new = 0
    with GHLClient() as ghl:
        ids = _resolve_stage(ghl)
        if not ids:
            return 0
        pipeline_id, stage_id = ids
        start_after = start_after_id = None
        for _ in range(20):  # hard page cap — the stage is small; never loop forever
            page = ghl.opportunities_page(
                pipeline_id=pipeline_id, limit=100, pipeline_stage_id=stage_id,
                start_after=start_after, start_after_id=start_after_id,
            )
            opps = page.get("opportunities", [])
            for o in opps:
                if o.get("pipelineStageId") and o.get("pipelineStageId") != stage_id:
                    continue
                changed = _parse_ghl_time(o.get("lastStageChangeAt") or o.get("updatedAt"))
                contact_id = o.get("contactId")
                if not contact_id or not changed or changed < since:
                    continue
                contact = o.get("contact") or {}
                _, created = register_deal(
                    db, contact_id, contact.get("name") or None, o.get("id"),
                    closed_at=min(changed, now),
                )
                new += created
            meta = page.get("meta") or {}
            if not opps or not meta.get("nextPage"):
                break
            start_after, start_after_id = meta.get("startAfter"), meta.get("startAfterId")
    return new


@celery_app.task(name="closed_deals.check")
def check_closed_deals() -> dict:
    s = get_settings()
    if not s.closed_deals_enabled:
        return {"status": "disabled"}
    db = SessionLocal()
    results: dict[str, str] = {}
    try:
        discovered = 0
        try:
            discovered = discover_closed_deals(db)
        except Exception:
            db.rollback()
            log.exception("closed deals: GHL discovery failed")
        pending = db.query(ClosedDeal).filter(ClosedDeal.status == ClosedDealStatus.pending).all()
        for deal in pending:
            try:
                results[deal.id] = process_deal(db, deal)
            except Exception as exc:
                db.rollback()
                deal.error_message = str(exc)[:1000]
                deal.updated_at = datetime.utcnow()
                db.commit()
                results[deal.id] = "error"
                log.exception("closed deal processing failed", extra={"deal": deal.id})
        return {"discovered": discovered, "checked": len(pending), "results": results}
    finally:
        db.close()
