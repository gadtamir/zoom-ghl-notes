"""GHL stage: find matching contact, create Note with the summary.

Strategy:
  1. Build candidate name queries by splitting the meeting_topic on +/_ separators.
     The Claude-extracted name from the transcript is a last-resort fallback.
  2. For each query, hit GHL contacts search and accumulate unique candidate contacts.
  3. If we know the meeting datetime (parsed from the topic), fetch each candidate's
     appointments and check whether any appointment startTime is within
     ±_APPOINTMENT_WINDOW_HOURS of the meeting datetime. Contacts with a matching
     appointment win, regardless of which employee owns the appointment — this
     gives us a single high-confidence signal without needing per-employee
     GHL user_id mapping in the DB.
  4. If nothing in the appointment-window check, fall back to the most-recently
     updated contact across all candidates (existing behaviour).
  5. No candidates → status=unmatched (summary kept; no note created).
"""

import logging
import re
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from ..models import Job, JobStatus
from ..services.anthropic_client import transliterate_name
from ..services.ghl_client import GHLClient


log = logging.getLogger(__name__)


_ZOOM_DT_PREFIX = re.compile(
    r"^\s*\d{4}-\d{2}-\d{2}[\s_T]+\d{1,2}[.\:_]\d{2}([.\:_]\d{2})?\s*[-_]*\s*",
)
# Captures `YYYY-MM-DD HH.MM(.SS)?` at the start of a Zoom folder/topic.
_MEETING_DT = re.compile(
    r"^\s*(\d{4})-(\d{2})-(\d{2})[\s_T]+(\d{1,2})[.\:_](\d{2})(?:[.\:_](\d{2}))?",
)
_GHL_DT_FORMATS = ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S")
# Generic Zoom-topic words to strip (so "פגישה עם דני" → "דני").
_GENERIC_WORDS = re.compile(
    r"(?:^|\s)(שיחה|פגישה|פגישת|זום|טלפון|טלפונית|עם|של|התאמה|אפיון|recording|meeting|zoom|call|with)(?=\s|$)",
    re.IGNORECASE,
)
# Separators that split a topic into independent candidate segments.
_SEGMENT_SPLIT = re.compile(r"\s*[+_]\s*")
# Strip parenthetical annotations like "אביאור (שם משפחה לא צוין)".
_PARENTHETICAL = re.compile(r"\s*\([^)]*\)")

_APPOINTMENT_WINDOW_HOURS = 6
_CONTACT_SEARCH_LIMIT_PER_QUERY = 10


def _clean_segment(seg: str) -> str:
    cleaned = _GENERIC_WORDS.sub(" ", seg)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" -")
    return cleaned


def _split_topic_into_candidates(topic: str | None) -> list[str]:
    if not topic:
        return []
    stripped = _ZOOM_DT_PREFIX.sub("", topic).strip()
    if not stripped:
        return []
    candidates: list[str] = []
    for seg in _SEGMENT_SPLIT.split(stripped):
        cleaned = _clean_segment(seg)
        if cleaned and len(cleaned) >= 2 and cleaned not in candidates:
            candidates.append(cleaned)
    return candidates


def _candidate_queries(job: Job) -> list[str]:
    out: list[str] = []
    out.extend(_split_topic_into_candidates(job.meeting_topic))
    if job.extracted_contact_name:
        name = _PARENTHETICAL.sub("", job.extracted_contact_name).strip()
        if name and name not in out:
            out.append(name)
    return out


def _parse_meeting_datetime(topic: str | None) -> datetime | None:
    """Pull the meeting datetime out of a Zoom folder name (naive, local TZ)."""
    if not topic:
        return None
    m = _MEETING_DT.match(topic)
    if not m:
        return None
    year, month, day, hour, minute = (int(m.group(i)) for i in range(1, 6))
    second = int(m.group(6)) if m.group(6) else 0
    try:
        return datetime(year, month, day, hour, minute, second)
    except ValueError:
        return None


def _parse_ghl_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    # GHL appointment times typically come as "YYYY-MM-DD HH:MM:SS" without timezone.
    # We treat both meeting_topic time and GHL time as naive in the same local TZ
    # since both originate from the same business location.
    raw = value.replace("Z", "").split(".")[0].strip()
    for fmt in _GHL_DT_FORMATS:
        try:
            return datetime.strptime(raw, fmt)
        except ValueError:
            continue
    return None


def _format_note(job: Job) -> str:
    date_display = job.meeting_date or job.created_at.strftime("%Y-%m-%d")
    title = f"📞 סיכום פגישת זום - {date_display} - הועלה ע\"י {job.employee_name}"
    body = job.summary or "(אין סיכום זמין)"
    return f"{title}\n\n{body}"


def _gather_candidates(ghl: GHLClient, queries: list[str], job_id: str) -> dict[str, dict]:
    """Run all queries and merge into a contact_id → contact dict.

    If no candidates were found after the initial pass, try transliteration
    variants (Hebrew↔English) for each query and search again. The original
    `query` field on each candidate records the spelling that actually matched.
    """
    by_id: dict[str, dict] = {}
    for q in queries:
        log.info("ghl search", extra={"job_id": job_id, "query": q})
        results = ghl.search_contacts(q, limit=_CONTACT_SEARCH_LIMIT_PER_QUERY)
        for c in results:
            cid = c.get("id")
            if not cid or cid in by_id:
                continue
            by_id[cid] = {"contact": c, "matched_by": q}

    if by_id:
        return by_id

    # No matches — try transliteration variants (only triggered when needed → cheap).
    for q in queries:
        try:
            variants = transliterate_name(q)
        except Exception:
            log.exception("transliterate failed", extra={"job_id": job_id, "query": q})
            continue
        for v in variants:
            log.info("ghl search (transliterated)", extra={"job_id": job_id, "original": q, "variant": v})
            try:
                results = ghl.search_contacts(v, limit=_CONTACT_SEARCH_LIMIT_PER_QUERY)
            except Exception:
                log.exception("ghl search failed", extra={"job_id": job_id, "query": v})
                continue
            for c in results:
                cid = c.get("id")
                if not cid or cid in by_id:
                    continue
                by_id[cid] = {"contact": c, "matched_by": f"{q} → {v}"}
    return by_id


def _score_by_appointment(
    ghl: GHLClient,
    candidates: dict[str, dict],
    meeting_dt: datetime,
    job_id: str,
) -> tuple[str | None, timedelta | None]:
    """For each candidate, check appointments and return the contact_id whose
    closest appointment is within the window. Ties: shorter delta wins.
    """
    best_id: str | None = None
    best_delta: timedelta | None = None
    window = timedelta(hours=_APPOINTMENT_WINDOW_HOURS)
    for cid, info in candidates.items():
        try:
            appts = ghl.get_contact_appointments(cid)
        except Exception:
            log.exception("appointment lookup failed", extra={"job_id": job_id, "contact_id": cid})
            continue
        for appt in appts:
            appt_dt = _parse_ghl_datetime(appt.get("startTime"))
            if not appt_dt:
                continue
            delta = abs(appt_dt - meeting_dt)
            if delta <= window and (best_delta is None or delta < best_delta):
                best_id = cid
                best_delta = delta
                info["matched_appointment"] = {
                    "id": appt.get("id"),
                    "startTime": appt.get("startTime"),
                    "calendarId": appt.get("calendarId"),
                    "delta_minutes": int(delta.total_seconds() // 60),
                }
    return best_id, best_delta


def attach_note(db: Session, job: Job) -> JobStatus:
    """Find contact via folder-split + calendar-aware scoring, create Note."""
    queries = _candidate_queries(job)
    if not queries:
        log.warning("no search candidates for job", extra={"job_id": job.id})
        job.status = JobStatus.unmatched
        job.completed_at = datetime.utcnow()
        db.commit()
        return JobStatus.unmatched

    meeting_dt = _parse_meeting_datetime(job.meeting_topic)
    log.info(
        "matching start",
        extra={"job_id": job.id, "queries": queries, "meeting_dt": meeting_dt.isoformat() if meeting_dt else None},
    )

    with GHLClient() as ghl:
        candidates = _gather_candidates(ghl, queries, job.id)
        if not candidates:
            log.info("no contacts found across any query", extra={"job_id": job.id})
            job.status = JobStatus.unmatched
            job.completed_at = datetime.utcnow()
            db.commit()
            return JobStatus.unmatched

        chosen_id: str | None = None
        chosen_reason = "fallback_recent"

        if meeting_dt is not None:
            chosen_id, delta = _score_by_appointment(ghl, candidates, meeting_dt, job.id)
            if chosen_id:
                chosen_reason = "appointment_window"
                log.info(
                    "matched by appointment window",
                    extra={
                        "job_id": job.id,
                        "contact_id": chosen_id,
                        "delta_minutes": int(delta.total_seconds() // 60) if delta else None,
                    },
                )

        if not chosen_id:
            # Fallback: most-recently-updated across all unique candidates
            sorted_ids = sorted(
                candidates.keys(),
                key=lambda c: candidates[c]["contact"].get("dateUpdated") or "",
                reverse=True,
            )
            chosen_id = sorted_ids[0]
            log.info(
                "matched by most-recent fallback",
                extra={"job_id": job.id, "contact_id": chosen_id, "candidates": len(candidates)},
            )

        chosen = candidates[chosen_id]["contact"]
        contact_name = chosen.get("contactName") or " ".join(
            filter(None, [chosen.get("firstName"), chosen.get("lastName")])
        )
        log.info(
            "contact selected",
            extra={
                "job_id": job.id,
                "contact_id": chosen_id,
                "contact_name": contact_name,
                "reason": chosen_reason,
                "matched_by": candidates[chosen_id]["matched_by"],
            },
        )

        note = ghl.create_note(contact_id=chosen_id, body=_format_note(job))
        note_id = note.get("id")

        job.ghl_contact_id = chosen_id
        job.ghl_note_id = note_id
        job.status = JobStatus.completed
        job.completed_at = datetime.utcnow()
        db.commit()

        log.info(
            "note created",
            extra={"job_id": job.id, "contact_id": chosen_id, "note_id": note_id},
        )
        return JobStatus.completed
