"""Background scanner service — runs in a thread, can be paused/resumed."""

import logging
import threading
import time
from datetime import datetime
from pathlib import Path

import httpx

from . import db
from .config import Config
from .uploader import handle_recording
from .watcher import scan


log = logging.getLogger(__name__)


class ScannerService:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self._stop_event = threading.Event()
        self._pause_event = threading.Event()
        self._wake_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._state_lock = threading.Lock()
        self.last_scan: datetime | None = None
        self.last_uploaded: datetime | None = None
        self.last_error: str | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._wake_event.clear()
        self._thread = threading.Thread(target=self._loop, name="zghl-scanner", daemon=True)
        self._thread.start()
        log.info("scanner started")

    def stop(self) -> None:
        self._stop_event.set()
        self._wake_event.set()
        if self._thread:
            self._thread.join(timeout=10)
        log.info("scanner stopped")

    def pause(self) -> None:
        self._pause_event.set()

    def resume(self) -> None:
        self._pause_event.clear()
        self._wake_event.set()

    @property
    def is_paused(self) -> bool:
        return self._pause_event.is_set()

    def scan_now(self) -> None:
        self._wake_event.set()

    def _loop(self) -> None:
        # Wait briefly so the tray icon shows up first.
        if self._stop_event.wait(timeout=2):
            return
        while not self._stop_event.is_set():
            if not self.is_paused:
                try:
                    self._scan_pass()
                except Exception as exc:
                    with self._state_lock:
                        self.last_error = str(exc)[:300]
                    log.exception("scan pass failed", extra={"err": str(exc)})
            # Sleep up to scan_interval, but allow wake from scan_now/stop/resume.
            self._wake_event.clear()
            interval_seconds = self.cfg.scan_interval_minutes * 60
            self._wake_event.wait(timeout=interval_seconds)

    def _scan_pass(self) -> None:
        watch = Path(self.cfg.watch_folder)
        log.info(f"scanning {watch}")
        recordings = scan(watch)
        with self._state_lock:
            self.last_scan = datetime.now()
        if not recordings:
            return

        with httpx.Client(timeout=httpx.Timeout(connect=10.0, read=600.0, write=600.0, pool=10.0)) as client:
            for rec in recordings:
                if self._stop_event.is_set():
                    return
                if db.was_uploaded(rec.file):
                    continue
                result = handle_recording(self.cfg, rec, client=client)
                if result.ok:
                    with self._state_lock:
                        self.last_uploaded = datetime.now()
                        self.last_error = None
                else:
                    with self._state_lock:
                        self.last_error = result.error or "unknown"

    def snapshot(self) -> dict:
        with self._state_lock:
            return {
                "paused": self.is_paused,
                "last_scan": self.last_scan.isoformat(timespec="seconds") if self.last_scan else None,
                "last_uploaded": self.last_uploaded.isoformat(timespec="seconds") if self.last_uploaded else None,
                "last_error": self.last_error,
            }
