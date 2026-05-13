"""Local SQLite tracking — remembers what was already uploaded."""

import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from .paths import local_db_path


SCHEMA = """
CREATE TABLE IF NOT EXISTS uploads (
    file_path TEXT PRIMARY KEY,         -- absolute path; the natural identity
    folder_name TEXT NOT NULL,           -- the parent folder = meeting_topic
    file_size INTEGER NOT NULL,
    server_job_id TEXT,                  -- returned by /upload
    status TEXT NOT NULL,                -- pending | uploaded | failed
    uploaded_at TEXT,                    -- ISO timestamp
    error_message TEXT,
    attempts INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_uploads_status ON uploads(status);
"""


@contextmanager
def connect():
    path = local_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    with connect() as conn:
        conn.executescript(SCHEMA)


def was_uploaded(file_path: Path) -> bool:
    with connect() as conn:
        row = conn.execute(
            "SELECT status FROM uploads WHERE file_path = ?", (str(file_path),)
        ).fetchone()
    return row is not None and row["status"] == "uploaded"


def mark_uploaded(file_path: Path, folder_name: str, job_id: str) -> None:
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO uploads (file_path, folder_name, file_size, server_job_id, status, uploaded_at, attempts)
            VALUES (?, ?, ?, ?, 'uploaded', ?, 1)
            ON CONFLICT(file_path) DO UPDATE SET
                server_job_id = excluded.server_job_id,
                status = 'uploaded',
                uploaded_at = excluded.uploaded_at,
                error_message = NULL,
                attempts = uploads.attempts + 1
            """,
            (
                str(file_path),
                folder_name,
                file_path.stat().st_size if file_path.exists() else 0,
                job_id,
                datetime.utcnow().isoformat(timespec="seconds"),
            ),
        )


def mark_failed(file_path: Path, folder_name: str, error: str) -> None:
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO uploads (file_path, folder_name, file_size, status, error_message, attempts)
            VALUES (?, ?, ?, 'failed', ?, 1)
            ON CONFLICT(file_path) DO UPDATE SET
                status = 'failed',
                error_message = excluded.error_message,
                attempts = uploads.attempts + 1
            """,
            (
                str(file_path),
                folder_name,
                file_path.stat().st_size if file_path.exists() else 0,
                error[:500],
            ),
        )


def stats() -> dict:
    with connect() as conn:
        rows = conn.execute(
            "SELECT status, COUNT(*) AS c FROM uploads GROUP BY status"
        ).fetchall()
        last = conn.execute(
            "SELECT folder_name, uploaded_at FROM uploads WHERE status='uploaded' ORDER BY uploaded_at DESC LIMIT 1"
        ).fetchone()
    out = {r["status"]: r["c"] for r in rows}
    out["last_upload"] = dict(last) if last else None
    return out
