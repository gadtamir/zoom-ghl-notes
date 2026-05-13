"""Main pipeline orchestrator.

Stages (status transitions):
    received → converted → transcribed → summarized → matched → completed
                                                              ↘ unmatched
    any-stage-error → failed
"""

import logging
from datetime import datetime
from pathlib import Path

from sqlalchemy.orm import Session

from ..config import get_settings
from ..db import SessionLocal
from ..models import Job, JobStatus
from .celery_app import celery_app
from .ghl import attach_note
from .media import is_audio, is_video, video_to_audio
from .summarize import summarize_job
from .transcribe import transcribe_audio


log = logging.getLogger(__name__)


def storage_path_for(job_id: str, original_filename: str) -> Path:
    """Deterministic on-disk path for a job's media file."""
    settings = get_settings()
    suffix = Path(original_filename).suffix.lower().lstrip(".") or "bin"
    return Path(settings.upload_dir) / f"{job_id}.{suffix}"


def _get_job(db: Session, job_id: str) -> Job:
    job = db.query(Job).filter(Job.id == job_id).first()
    if not job:
        raise RuntimeError(f"Job {job_id} not found")
    return job


def _set_status(db: Session, job: Job, status: JobStatus) -> None:
    job.status = status
    if status in (JobStatus.completed, JobStatus.failed, JobStatus.unmatched):
        job.completed_at = datetime.utcnow()
    db.commit()


@celery_app.task(name="pipeline.run", bind=True, max_retries=0)
def run_pipeline(self, job_id: str) -> dict:
    """Orchestrate all stages. Step 2 implements only file save + convert.
    Later stages (transcribe / summarize / GHL) are added incrementally.
    """
    db = SessionLocal()
    try:
        job = _get_job(db, job_id)
        job.attempts += 1
        db.commit()

        try:
            audio_path = _stage_convert(db, job)
        except Exception as exc:
            log.exception("convert failed", extra={"job_id": job_id})
            job.error_message = f"convert: {exc}"
            _set_status(db, job, JobStatus.failed)
            return {"job_id": job_id, "status": "failed", "stage": "convert"}

        try:
            _stage_transcribe(db, job, audio_path)
        except Exception as exc:
            log.exception("transcribe failed", extra={"job_id": job_id})
            job.error_message = f"transcribe: {exc}"
            _set_status(db, job, JobStatus.failed)
            return {"job_id": job_id, "status": "failed", "stage": "transcribe"}

        try:
            summarize_job(db, job)
        except Exception as exc:
            log.exception("summarize failed", extra={"job_id": job_id})
            job.error_message = f"summarize: {exc}"
            _set_status(db, job, JobStatus.failed)
            return {"job_id": job_id, "status": "failed", "stage": "summarize"}

        try:
            final = attach_note(db, job)
        except Exception as exc:
            log.exception("ghl failed", extra={"job_id": job_id})
            job.error_message = f"ghl: {exc}"
            _set_status(db, job, JobStatus.failed)
            return {"job_id": job_id, "status": "failed", "stage": "ghl"}

        _cleanup_audio(audio_path, job_id)
        return {
            "job_id": job_id,
            "status": final.value,
            "ghl_contact_id": job.ghl_contact_id,
            "ghl_note_id": job.ghl_note_id,
        }
    finally:
        db.close()


def _stage_convert(db: Session, job: Job) -> Path:
    src = storage_path_for(job.id, job.original_filename)
    if not src.exists():
        raise RuntimeError(f"source file missing on disk: {src}")

    if is_audio(src):
        log.info("source is already audio — no conversion", extra={"job_id": job.id})
        _set_status(db, job, JobStatus.converted)
        return src

    if is_video(src):
        dst = src.with_suffix(".m4a")
        video_to_audio(src, dst)
        try:
            src.unlink()
        except OSError:
            log.warning("could not delete source video", extra={"src": str(src)})
        _set_status(db, job, JobStatus.converted)
        return dst

    raise RuntimeError(f"unsupported file extension: {src.suffix}")


def _cleanup_audio(audio_path: Path, job_id: str) -> None:
    """Best-effort cleanup of the audio file after the pipeline completes."""
    try:
        if audio_path.exists():
            audio_path.unlink()
            log.info("audio cleaned up", extra={"job_id": job_id, "path": str(audio_path)})
    except OSError as exc:
        log.warning("audio cleanup failed", extra={"job_id": job_id, "error": str(exc)})


def _stage_transcribe(db: Session, job: Job, audio_path: Path) -> str:
    """Transcribe with OpenAI gpt-4o-transcribe. Stores transcript on job."""
    text = transcribe_audio(audio_path, language="he")
    job.transcript = text
    _set_status(db, job, JobStatus.transcribed)
    return text
