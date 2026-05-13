import logging
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status

from ..auth import get_current_employee
from ..config import get_settings
from ..db import SessionLocal
from ..models import Employee, Job, JobStatus
from ..tasks.pipeline import run_pipeline, storage_path_for


log = logging.getLogger(__name__)
router = APIRouter(prefix="/upload", tags=["upload"])

CHUNK = 1024 * 1024  # 1 MiB streaming


def _allowed_exts() -> set[str]:
    return {e.strip().lower() for e in get_settings().allowed_extensions.split(",") if e.strip()}


@router.post("", status_code=status.HTTP_202_ACCEPTED)
async def upload_recording(
    file: UploadFile = File(..., description="Audio or video recording"),
    original_filename: str = Form(..., description="Original filename as it was on the employee's machine"),
    meeting_topic: str | None = Form(None, description="Meeting topic — typically the Zoom folder name"),
    meeting_date: str | None = Form(None, description="ISO date, e.g. 2026-05-13"),
    employee: Employee = Depends(get_current_employee),
):
    settings = get_settings()
    ext = Path(original_filename).suffix.lower().lstrip(".")
    if ext not in _allowed_exts():
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail=f"Extension '.{ext}' not allowed. Allowed: {sorted(_allowed_exts())}",
        )

    db = SessionLocal()
    try:
        job = Job(
            employee_id=employee.id,
            employee_name=employee.name,
            original_filename=original_filename,
            meeting_topic=meeting_topic,
            meeting_date=meeting_date,
            status=JobStatus.received,
        )
        db.add(job)
        db.commit()
        db.refresh(job)
        job_id = job.id
    finally:
        db.close()

    dst = storage_path_for(job_id, original_filename)
    dst.parent.mkdir(parents=True, exist_ok=True)

    max_bytes = settings.max_upload_mb * 1024 * 1024
    written = 0
    try:
        with dst.open("wb") as out:
            while True:
                chunk = await file.read(CHUNK)
                if not chunk:
                    break
                written += len(chunk)
                if written > max_bytes:
                    out.close()
                    dst.unlink(missing_ok=True)
                    _mark_failed(job_id, f"file exceeds max_upload_mb={settings.max_upload_mb}")
                    raise HTTPException(
                        status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                        detail=f"File exceeds limit ({settings.max_upload_mb} MB)",
                    )
                out.write(chunk)
    except HTTPException:
        raise
    except Exception as exc:
        log.exception("write failed", extra={"job_id": job_id})
        dst.unlink(missing_ok=True)
        _mark_failed(job_id, f"write: {exc}")
        raise HTTPException(status_code=500, detail="Failed to store upload") from exc

    log.info("upload stored", extra={"job_id": job_id, "bytes": written, "path": str(dst)})
    run_pipeline.delay(job_id)
    return {"job_id": job_id, "status": JobStatus.received.value, "bytes": written}


def _mark_failed(job_id: str, msg: str) -> None:
    db = SessionLocal()
    try:
        job = db.query(Job).filter(Job.id == job_id).first()
        if job:
            job.status = JobStatus.failed
            job.error_message = msg
            db.commit()
    finally:
        db.close()
