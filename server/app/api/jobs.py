from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..auth import get_current_employee
from ..db import get_db
from ..models import Employee, Job, JobStatus

router = APIRouter(prefix="/jobs", tags=["jobs"])


class JobOut(BaseModel):
    id: str
    employee_name: str
    original_filename: str
    meeting_topic: str | None
    meeting_date: str | None
    status: JobStatus
    extracted_contact_name: str | None
    ghl_contact_id: str | None
    error_message: str | None
    created_at: datetime
    completed_at: datetime | None

    model_config = {"from_attributes": True}


class JobDetail(JobOut):
    transcript: str | None
    summary: str | None
    ghl_note_id: str | None


@router.get("/{job_id}", response_model=JobDetail)
def get_job(
    job_id: str,
    db: Session = Depends(get_db),
    employee: Employee = Depends(get_current_employee),
) -> JobDetail:
    job = db.query(Job).filter(Job.id == job_id).first()
    if not job:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")
    if job.employee_id != employee.id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not your job")
    return JobDetail.model_validate(job)


@router.get("", response_model=list[JobOut])
def list_jobs(
    limit: int = Query(default=50, le=200),
    db: Session = Depends(get_db),
    employee: Employee = Depends(get_current_employee),
) -> list[JobOut]:
    rows = (
        db.query(Job)
        .filter(Job.employee_id == employee.id)
        .order_by(Job.created_at.desc())
        .limit(limit)
        .all()
    )
    return [JobOut.model_validate(r) for r in rows]
