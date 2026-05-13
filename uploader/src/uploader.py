"""Upload a recording to the server."""

import logging
import shutil
from pathlib import Path

import httpx

from . import db
from .config import Config
from .watcher import Recording


log = logging.getLogger(__name__)


class UploadResult:
    def __init__(self, ok: bool, job_id: str | None = None, error: str | None = None):
        self.ok = ok
        self.job_id = job_id
        self.error = error


def upload_recording(cfg: Config, rec: Recording, client: httpx.Client | None = None) -> UploadResult:
    own_client = client is None
    client = client or httpx.Client(timeout=httpx.Timeout(connect=10.0, read=600.0, write=600.0, pool=10.0))
    try:
        with rec.file.open("rb") as f:
            files = {"file": (rec.file.name, f, "application/octet-stream")}
            data = {
                "original_filename": rec.file.name,
                "meeting_topic": rec.folder_name,
                "meeting_date": rec.meeting_date,
            }
            r = client.post(
                f"{cfg.server_url.rstrip('/')}/upload",
                headers={"X-API-Key": cfg.api_key},
                files=files,
                data=data,
            )
        if r.status_code in (200, 202):
            job_id = r.json().get("job_id")
            log.info("uploaded", extra={"file": str(rec.file), "job_id": job_id})
            return UploadResult(ok=True, job_id=job_id)
        return UploadResult(ok=False, error=f"HTTP {r.status_code}: {r.text[:300]}")
    except httpx.HTTPError as exc:
        return UploadResult(ok=False, error=f"network: {exc}")
    finally:
        if own_client:
            client.close()


def move_to_uploaded(rec: Recording) -> Path | None:
    """Move the recording folder to <watch_folder>/uploaded/. Returns new path or None on failure."""
    parent = rec.folder.parent
    uploaded_dir = parent / "uploaded"
    uploaded_dir.mkdir(exist_ok=True)
    dst = uploaded_dir / rec.folder.name
    if dst.exists():
        # Folder name collision — append a numeric suffix.
        for i in range(2, 100):
            alt = uploaded_dir / f"{rec.folder.name} ({i})"
            if not alt.exists():
                dst = alt
                break
    try:
        shutil.move(str(rec.folder), str(dst))
        log.info("moved to uploaded/", extra={"from": str(rec.folder), "to": str(dst)})
        return dst
    except OSError as exc:
        log.warning("move failed", extra={"err": str(exc)})
        return None


def handle_recording(cfg: Config, rec: Recording, client: httpx.Client | None = None) -> UploadResult:
    if db.was_uploaded(rec.file):
        return UploadResult(ok=True, job_id=None, error="already_uploaded")
    result = upload_recording(cfg, rec, client=client)
    if result.ok and result.job_id:
        db.mark_uploaded(rec.file, rec.folder_name, result.job_id)
        if cfg.move_after_upload:
            move_to_uploaded(rec)
    else:
        db.mark_failed(rec.file, rec.folder_name, result.error or "unknown error")
    return result
