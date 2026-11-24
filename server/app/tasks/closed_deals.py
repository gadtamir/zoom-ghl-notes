"""Won / lost leads → one Google Doc of sales-meeting transcripts per lead.

Flow:
  1. `closed_deals.check` (Celery beat, every 15 min) polls GHL for opportunities in
     pipeline "ליד ראשי" that moved into "ליד נסגר" (won) or "לא רלוונטי" (lost)
     within the lookback, and records a `ClosedDeal` for each one that has a sales
     meeting on record. (A GHL workflow can also POST to /webhooks/ghl/deal-closed.)
  2. The same task walks the pending deals. Nothing is written for the first hour
     after the stage change — the lead is often moved on the call that is still
     being transcribed. After that the doc is created as soon as no matching meeting
     is still mid-pipeline, and rewritten whenever a transcript or the reason field
     changes, until the window (24h) ends.

Each doc holds: what the lead said they want solved (extracted by Claude from the
transcripts), the opportunity's "why it closed / didn't close" field, and the full
transcripts. Won docs and lost docs go to separate Drive folders.

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

WON, LOST = "won", "lost"

# A lead registered twice for the same outcome (poll overlap, workflow re-entry,
# manual re-run) must not produce a second doc.
_DEDUPE_WINDOW = timedelta(days=7)

# Meetings in these states will never get a transcript, so they don't hold the
# doc back.
_ZOOM_DEAD = {ZoomMeetingStatus.skipped, ZoomMeetingStatus.failed}
_JOB_DEAD = {JobStatus.failed, JobStatus.unmatched}

# Page cap per stage per poll (100 opportunities a page). "לא רלוונטי" is a big
# stage; if the cap is ever hit we log it rather than loop for minutes.
_MAX_PAGES = 50

# Claude's context is large but not infinite; several long meetings fit well
# under this.
_PAIN_MAX_CHARS = 400_000
PAIN_NOT_ASKED = "השאלה לא נשאלה בפגישה"


@dataclass(frozen=True)
class _Outcome:
    key: str
    label: str            # heading of the reason section
    status_line: str      # "נסגר" / "לא נסגר"

    @property
    def stage_name(self) -> str:
        s = get_settings()
        return s.closed_deals_stage_name if self.key == WON else s.closed_deals_lost_stage_name

    @property
    def folder_id(self) -> str:
        s = get_settings()
        return s.closed_deals_won_folder_id if self.key == WON else s.closed_deals_lost_folder_id

    @property
    def reason_field(self) -> str:
        s = get_settings()
        return s.closed_deals_won_reason_field if self.key == WON else s.closed_deals_lost_reason_field


OUTCOMES = {
    WON: _Outcome(WON, "למה הליד נסגר", "נסגר"),
    LOST: _Outcome(LOST, "למה הליד לא נסגר", "לא נסגר (לא רלוונטי)"),
}


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
        if not kind or (zm.status in _ZOOM_DEAD and not zm.transcript):
            continue
        out.append(_Meeting(
            key=f"zoom:{zm.id}", kind=kind, started_at=_il(zm.started_at),
            duration_minutes=zm.duration_minutes or None, transcript=zm.transcript,
            in_progress=not zm.transcript,
        ))
    for job in db.query(Job).filter(Job.ghl_contact_id == contact_id).all():
        kind = sales_meeting_type(job.meeting_topic or job.original_filename)
        if not kind or (job.status in _JOB_DEAD and not job.transcript):
            continue
        out.append(_Meeting(
            key=f"job:{job.id}", kind=kind, started_at=_job_date(job),
            duration_minutes=None, transcript=job.transcript,
            in_progress=not job.transcript,
        ))
    out.sort(key=lambda m: m.started_at or datetime.min)
    return out


# --- rendering -----------------------------------------------------------------

_P = '<p dir="rtl" style="text-align:right">'
_H2 = '<h2 dir="rtl" style="text-align:right">'


def _paragraphs(text: str) -> str:
    parts = [p.strip() for p in re.split(r"\n\s*\n|\n", text) if p.strip()]
    return "\n".join(f"{_P}{html.escape(p)}</p>" for p in parts)


def _bullets(text: str) -> str:
    """Lines starting with '-' become a list; anything else stays a paragraph."""
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    items = [re.sub(r"^[-•*]\s*", "", ln) for ln in lines if re.match(r"^[-•*]\s*", ln)]
    if items and len(items) == len(lines):
        lis = "".join(f'<li dir="rtl" style="text-align:right">{html.escape(i)}</li>' for i in items)
        return f'<ul dir="rtl">{lis}</ul>'
    return _paragraphs(text)


def _meeting_heading(m: _Meeting) -> str:
    when = "תאריך לא ידוע"
    if m.started_at:
        when = f"{m.started_at:%d/%m/%Y}" if m.key.startswith("job:") else f"{m.started_at:%d/%m/%Y %H:%M}"
    dur = f" · {m.duration_minutes} דק'" if m.duration_minutes else ""
    return f"{m.kind} — {when}{dur}"


def render_doc(
    name: str,
    outcome: _Outcome,
    closed_at_il: datetime,
    meetings: list[_Meeting],
    pain_points: str | None,
    reason: str | None,
) -> str:
    blocks = [
        f'<h1 dir="rtl" style="text-align:right">{html.escape(name)}</h1>',
        f"{_P}<b>סטטוס:</b> {outcome.status_line} · {closed_at_il:%d/%m/%Y} · {len(meetings)} פגישות</p>",
        f"{_H2}למה הלקוח השאיר פרטים</h2>",
        _bullets(pain_points) if pain_points else f"{_P}לא זמין</p>",
        f"{_H2}{outcome.label}</h2>",
        _paragraphs(reason) if reason else f"{_P}השדה לא מולא ב-GHL</p>",
    ]
    for m in meetings:
        blocks.append(f"{_H2}תמלול: {html.escape(_meeting_heading(m))}</h2>")
        blocks.append(_paragraphs(m.transcript or ""))
    return (
        '<html><head><meta charset="utf-8"></head><body dir="rtl">'
        + "\n".join(blocks)
        + "</body></html>"
    )


# --- registering ---------------------------------------------------------------

def register_deal(
    db: Session,
    contact_id: str,
    name: str | None,
    opportunity_id: str | None,
    closed_at: datetime | None = None,
    outcome: str = WON,
) -> tuple[ClosedDeal, bool]:
    """Record a won/lost lead. Returns (deal, created). Idempotent per contact and
    outcome within the dedupe window."""
    since = datetime.utcnow() - _DEDUPE_WINDOW
    existing = (
        db.query(ClosedDeal)
        .filter(
            ClosedDeal.ghl_contact_id == contact_id,
            ClosedDeal.outcome == outcome,
            ClosedDeal.closed_at >= since,
        )
        .order_by(ClosedDeal.closed_at.desc())
        .first()
    )
    if existing:
        if opportunity_id and not existing.ghl_opportunity_id:
            existing.ghl_opportunity_id = opportunity_id
            db.commit()
        return existing, False
    deal = ClosedDeal(
        ghl_contact_id=contact_id, contact_name=name, ghl_opportunity_id=opportunity_id, outcome=outcome,
    )
    if closed_at:
        deal.closed_at = closed_at
    db.add(deal)
    db.commit()
    db.refresh(deal)
    log.info("lead doc registered", extra={"deal": deal.id, "contact": contact_id, "outcome": outcome})
    return deal, True


# --- GHL lookups ---------------------------------------------------------------

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
        log.warning("lead doc: contact name lookup failed", extra={"deal": deal.id, "error": str(exc)})
    return deal.ghl_contact_id


def _field_value(cf: dict) -> str | None:
    """A custom field's value as text. GHL names the key differently per field
    type and endpoint, so try them all."""
    for key in ("fieldValue", "fieldValueString", "fieldValueLargeText", "value", "fieldValueArray"):
        v = cf.get(key)
        if isinstance(v, (list, tuple)):
            v = ", ".join(str(x) for x in v if x)
        if v not in (None, ""):
            return str(v).strip() or None
    return None


def fetch_reason(deal: ClosedDeal) -> str | None:
    """The opportunity's reason field for this outcome. Raises on API failure so
    the caller can keep the previous value instead of blanking it."""
    if not deal.ghl_opportunity_id:
        return None
    from ..services.ghl_client import GHLClient

    want = OUTCOMES[deal.outcome].reason_field.strip()
    with GHLClient() as ghl:
        field_id = next(
            (f.get("id") for f in ghl.opportunity_custom_fields() if (f.get("name") or "").strip() == want),
            None,
        )
        if not field_id:
            log.warning("lead doc: opportunity field not found", extra={"field": want})
            return None
        opp = ghl.get_opportunity(deal.ghl_opportunity_id) or {}
    for cf in opp.get("customFields") or []:
        if cf.get("id") == field_id:
            return _field_value(cf)
    return None


def _pain_points(deal: ClosedDeal, ready: list[_Meeting], ready_keys: set[str]) -> str | None:
    """What the lead wants solved, re-extracted only when the transcripts changed.
    A failed extraction keeps the previous text and is retried on the next check."""
    if deal.pain_points and set(json.loads(deal.pain_keys or "[]")) == ready_keys:
        return deal.pain_points
    try:
        from ..services.anthropic_client import extract_lead_goals

        text = "\n\n".join(f"=== {_meeting_heading(m)} ===\n{m.transcript}" for m in ready)
        deal.pain_points = extract_lead_goals(text[:_PAIN_MAX_CHARS]) or PAIN_NOT_ASKED
        deal.pain_keys = json.dumps(sorted(ready_keys))
    except Exception as exc:
        log.warning("lead doc: goal extraction failed", extra={"deal": deal.id, "error": str(exc)})
    return deal.pain_points


# --- processing ----------------------------------------------------------------

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

    result = "unchanged"
    hold = busy and now < deadline and not force
    if hold:
        result = "waiting_transcripts"
    elif ready:
        try:
            reason = fetch_reason(deal)
        except Exception as exc:
            reason = deal.reason
            log.warning("lead doc: reason lookup failed", extra={"deal": deal.id, "error": str(exc)})
        pain_before = deal.pain_points
        pain = _pain_points(deal, ready, ready_keys)
        # Persist the extraction now: if the Drive write below fails, the retry
        # must not pay for the same Claude call again.
        db.commit()

        if ready_keys != included or reason != deal.reason or pain != pain_before or not deal.gdoc_id:
            outcome = OUTCOMES[deal.outcome]
            name = _resolve_name(deal)
            closed_il = _il(deal.closed_at)
            from ..services.gdrive_client import upsert_html_gdoc

            f = upsert_html_gdoc(
                f"{name} - {closed_il:%d.%m.%Y}",
                render_doc(name, outcome, closed_il, ready, pain, reason),
                outcome.folder_id,
                file_id=deal.gdoc_id,
            )
            result = "doc_updated" if deal.gdoc_id else "doc_created"
            deal.gdoc_id = f["id"]
            deal.gdoc_link = f.get("webViewLink") or deal.gdoc_link
            deal.included_keys = json.dumps(sorted(ready_keys))
            deal.reason = reason
            log.info("lead doc written", extra={
                "deal": deal.id, "outcome": deal.outcome, "meetings": len(ready),
                "doc": deal.gdoc_id, "result": result,
            })

    if (now >= deadline and not busy) or now >= deadline + timedelta(hours=s.closed_deals_window_hours):
        # Past the window (or far past it, if a meeting is stuck mid-pipeline).
        deal.status = ClosedDealStatus.done if deal.gdoc_id else ClosedDealStatus.no_meetings
        deal.completed_at = now
        result = f"{result}+{deal.status.value}"

    deal.error_message = None
    deal.updated_at = now
    db.commit()
    return result


# --- discovery -----------------------------------------------------------------

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


def _resolve_stages(ghl) -> tuple[str, dict[str, str]] | None:
    """(pipeline_id, {outcome: stage_id}) by name. Exact stage-name match wins;
    otherwise a case-insensitive substring match (stage names carry emojis)."""
    s = get_settings()
    want_p = s.closed_deals_pipeline_name.strip().lower()
    for p in ghl.list_pipelines():
        if want_p not in (p.get("name") or "").lower():
            continue
        stages = p.get("stages", [])
        found: dict[str, str] = {}
        for o in OUTCOMES.values():
            want = o.stage_name.strip().lower()
            if not want:
                continue
            names = [((st.get("name") or "").strip().lower(), st.get("id")) for st in stages]
            match = next((i for n, i in names if n == want), None) or next((i for n, i in names if want in n), None)
            if match:
                found[o.key] = match
            else:
                log.error("lead docs: stage not found", extra={"stage": o.stage_name})
        return p["id"], found
    log.error("lead docs: pipeline not found", extra={"pipeline": s.closed_deals_pipeline_name})
    return None


def discover_closed_deals(db: Session, now: datetime | None = None) -> int:
    """Register every opportunity that moved into the won or lost stage within the
    lookback window and has a sales meeting on record. Returns the number of new
    registrations.

    The meeting check keeps "לא רלוונטי" (mostly leads who never had a meeting)
    from flooding the table; a meeting that lands later is picked up by a later
    poll, as long as it is inside the lookback."""
    from ..services.ghl_client import GHLClient

    s = get_settings()
    now = now or datetime.utcnow()
    since = now - timedelta(hours=s.closed_deals_poll_lookback_hours)
    new = 0
    with GHLClient() as ghl:
        resolved = _resolve_stages(ghl)
        if not resolved:
            return 0
        pipeline_id, stage_ids = resolved
        for outcome, stage_id in stage_ids.items():
            start_after = start_after_id = None
            for page_no in range(_MAX_PAGES):
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
                    if not collect_meetings(db, contact_id):
                        continue
                    _, created = register_deal(
                        db, contact_id, (o.get("contact") or {}).get("name") or None, o.get("id"),
                        closed_at=min(changed, now), outcome=outcome,
                    )
                    new += created
                meta = page.get("meta") or {}
                if not opps or not meta.get("nextPage"):
                    break
                start_after, start_after_id = meta.get("startAfter"), meta.get("startAfterId")
                if page_no == _MAX_PAGES - 1:
                    log.warning("lead docs: page cap hit, stage not fully scanned", extra={"outcome": outcome})
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
            log.exception("lead docs: GHL discovery failed")
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
                log.exception("lead doc processing failed", extra={"deal": deal.id})
        return {"discovered": discovered, "checked": len(pending), "results": results}
    finally:
        db.close()
