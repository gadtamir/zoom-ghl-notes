"""GHL stage: find matching contact, create Note with the summary.

Strategy:
  1. Build candidate search queries in priority order:
     a. extracted_contact_name (from Claude)
     b. parsed meeting_topic (with Zoom datetime prefix stripped)
  2. For each query, hit GHL contacts search.
     - If matches found: pick the most-recently-updated one and we're done.
     - If multiple matches: warn in log, still pick most-recent.
  3. If no candidate yielded a match → status=unmatched (summary kept; no note created).
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
_GENERIC_WORDS = re.compile(
    r"\b(שיחה|פגישה|פגישת|זום|זום_|טלפון|טלפונית|עם|עם_|של|recording|meeting|zoom|call|with)\b",
    re.IGNORECASE,
)


def _clean_topic(topic: str | None) -> str | None:
    if not topic:
        return None
    cleaned = _ZOOM_DT_PREFIX.sub("", topic)
    cleaned = _GENERIC_WORDS.sub("", cleaned)
    cleaned = re.sub(r"[\s_\-]+", " ", cleaned).strip()
    return cleaned or None


def _candidate_queries(job: Job) -> list[str]:
    out: list[str] = []
    if job.extracted_contact_name:
        out.append(job.extracted_contact_name.strip())
    cleaned_topic = _clean_topic(job.meeting_topic)
    if cleaned_topic and cleaned_topic not in out:
        out.append(cleaned_topic)
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
