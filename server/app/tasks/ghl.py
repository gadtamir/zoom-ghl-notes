"""GHL stage: find matching contact, create Note with the summary.

Strategy:
  1. Build candidate search queries in priority order:
     a. parsed meeting_topic (folder name with Zoom datetime prefix stripped) — PRIMARY signal
     b. extracted_contact_name (from Claude, transcript) — last-resort fallback only
  2. For each query, hit GHL contacts search.
     - If matches found: pick the most-recently-updated one and we're done.
     - If multiple matches: warn in log, still pick most-recent.
  3. If no candidate yielded a match → status=unmatched (summary kept; no note created).

Planned next iteration: cross-check against GHL calendar appointments by
(employee, meeting_datetime). When implemented, calendar will become priority 1
and folder name becomes priority 2.
"""

import logging
import re
from datetime import datetime

from sqlalchemy.orm import Session

from ..models import Job, JobStatus
from ..services.ghl_client import GHLClient


log = logging.getLogger(__name__)


_ZOOM_DT_PREFIX = re.compile(
    r"^\s*\d{4}-\d{2}-\d{2}[\s_T]+\d{1,2}[.\:_]\d{2}([.\:_]\d{2})?\s*[-_]*\s*",
)
# Words that almost always appear in Zoom meeting topics but are not part of any
# contact's name. We DROP these tokens (so "פגישה עם דני" → "דני").
_GENERIC_WORDS = re.compile(
    r"(?:^|\s)(שיחה|פגישה|פגישת|זום|טלפון|טלפונית|עם|של|התאמה|אפיון|recording|meeting|zoom|call|with)(?=\s|$)",
    re.IGNORECASE,
)
# Separators that split a Zoom topic into independent candidate segments.
# "+" — almost always "and" between two parties
# "_" — common filename separator
# We intentionally exclude "-" because legitimate names contain it (e.g. "More-Than").
_SEGMENT_SPLIT = re.compile(r"\s*[+_]\s*")
# Strip parenthetical annotations from Claude-extracted names like "אביאור (שם משפחה לא צוין)".
_PARENTHETICAL = re.compile(r"\s*\([^)]*\)")


def _clean_segment(seg: str) -> str:
    """Apply generic-word removal and whitespace collapse to a single segment."""
    cleaned = _GENERIC_WORDS.sub(" ", seg)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" -")
    return cleaned


def _split_topic_into_candidates(topic: str | None) -> list[str]:
    """Return ordered candidate names extracted from a Zoom meeting topic.

    First strips the leading Zoom datetime, then splits on `+`/`_` separators
    (each side is typically a separate party), then drops generic meeting
    words from each segment. The first segment is usually the client name in
    Zoom's "<client> + <employee> - <company>" pattern.
    """
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


def _format_note(job: Job) -> str:
    date_display = job.meeting_date or job.created_at.strftime("%Y-%m-%d")
    title = f"📞 סיכום פגישת זום - {date_display} - הועלה ע\"י {job.employee_name}"
    body = job.summary or "(אין סיכום זמין)"
    return f"{title}\n\n{body}"


def attach_note(db: Session, job: Job) -> JobStatus:
    """Find contact via GHL search, create Note. Returns the resulting JobStatus."""
    queries = _candidate_queries(job)
    if not queries:
        log.warning("no search candidates for job", extra={"job_id": job.id})
        job.status = JobStatus.unmatched
        job.completed_at = datetime.utcnow()
        db.commit()
        return JobStatus.unmatched

    with GHLClient() as ghl:
        best_contact: dict | None = None
        used_query: str | None = None
        for q in queries:
            log.info("ghl search", extra={"job_id": job.id, "query": q})
            try:
                results = ghl.search_contacts(q)
            except Exception as exc:
                log.exception("ghl search failed", extra={"job_id": job.id, "query": q})
                raise
            if not results:
                continue
            if len(results) > 1:
                log.warning(
                    "multiple contacts matched — picking most recently updated",
                    extra={"job_id": job.id, "query": q, "count": len(results)},
                )
            # Sort by dateUpdated descending; missing fields go last.
            results.sort(key=lambda c: c.get("dateUpdated") or "", reverse=True)
            best_contact = results[0]
            used_query = q
            break

        if not best_contact:
            log.info("no matching contact — marking unmatched", extra={"job_id": job.id})
            job.status = JobStatus.unmatched
            job.completed_at = datetime.utcnow()
            db.commit()
            return JobStatus.unmatched

        contact_id = best_contact["id"]
        contact_name = best_contact.get("contactName") or best_contact.get("firstName", "") + " " + best_contact.get("lastName", "")
        log.info(
            "contact matched",
            extra={"job_id": job.id, "contact_id": contact_id, "contact_name": contact_name, "query": used_query},
        )

        note_body = _format_note(job)
        note = ghl.create_note(contact_id=contact_id, body=note_body)
        note_id = note.get("id")

        job.ghl_contact_id = contact_id
        job.ghl_note_id = note_id
        job.status = JobStatus.completed
        job.completed_at = datetime.utcnow()
        db.commit()

        log.info(
            "note created",
            extra={"job_id": job.id, "contact_id": contact_id, "note_id": note_id},
        )
        return JobStatus.completed
