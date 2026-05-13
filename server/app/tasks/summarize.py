"""Summarize stage: call Claude opus-4-5, extract a likely contact name as a side-product."""

import logging

from sqlalchemy.orm import Session

from ..models import Job, JobStatus
from ..services.anthropic_client import summarize_meeting


log = logging.getLogger(__name__)


def summarize_job(db: Session, job: Job) -> str:
    if not job.transcript:
        raise RuntimeError("cannot summarize: transcript missing")

    summary, contact_name = summarize_meeting(
        transcript=job.transcript,
        employee_name=job.employee_name,
    )
    job.summary = summary
    job.extracted_contact_name = contact_name
    job.status = JobStatus.summarized
    db.commit()

    log.info(
        "summarize complete",
        extra={
            "job_id": job.id,
            "extracted_contact": contact_name,
            "summary_chars": len(summary),
        },
    )
    return summary
