"""Tray-icon entry point — the main app the user sees.

Runs the scanner in a background thread, exposes a menu in the system tray
(NSStatusItem on macOS, system tray on Windows).
The Settings window is spawned as a subprocess (Tkinter needs the main thread).
"""

import logging
import logging.handlers
import os
import subprocess
import sys
import threading
import webbrowser
from pathlib import Path

import pystray

from . import APP_NAME, __version__, db
from .config import Config
from .icon import make_icon
from .paths import app_data_dir, log_path
from .service import ScannerService


def _configure_logging() -> None:
    """Log to file (rotating) + stdout when run from terminal."""
    handlers: list[logging.Handler] = [
        logging.handlers.RotatingFileHandler(log_path(), maxBytes=2_000_000, backupCount=3, encoding="utf-8"),
    ]
    if sys.stdout.isatty():
        handlers.append(logging.StreamHandler(sys.stdout))
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        handlers=handlers,
    )


log = logging.getLogger(__name__)


def _spawn_settings() -> None:
    """Open the settings window in a new process so Tk gets the main thread."""
    # When frozen (PyInstaller), we re-exec ourselves with --settings flag.
    # When dev, we invoke python -m src.settings_window.
    if getattr(sys, "frozen", False):
        subprocess.Popen([sys.executable, "--settings"])
    else:
        env = os.environ.copy()
        subprocess.Popen([sys.executable, "-m", "src.settings_window"], env=env, cwd=Path(__file__).resolve().parent.parent)


def _open_log() -> None:
    webbrowser.open(log_path().as_uri())


def _open_data_dir() -> None:
    webbrowser.open(app_data_dir().as_uri())


def build_menu(icon: pystray.Icon, service: ScannerService) -> pystray.Menu:
    def status_text(item):
        snap = service.snapshot()
        if snap["paused"]:
            return "⏸ מושהה"
        if snap["last_error"]:
            return f"⚠ שגיאה: {snap['last_error'][:40]}"
        if snap["last_uploaded"]:
            return f"✓ העלאה אחרונה: {snap['last_uploaded']}"
        if snap["last_scan"]:
            return f"⌛ סריקה אחרונה: {snap['last_scan']}"
        return "מתחיל…"

    def last_scan_text(item):
        snap = service.snapshot()
        return f"סריקה אחרונה: {snap['last_scan'] or '—'}"

    def toggle_pause(icon: pystray.Icon, item: pystray.MenuItem):
        if service.is_paused:
            service.resume()
        else:
            service.pause()
        icon.icon = make_icon(active=not service.is_paused)
        icon.update_menu()

    def scan_now(icon, item):
        service.scan_now()

    def open_settings(icon, item):
        _spawn_settings()

    def quit_app(icon, item):
        service.stop()
        icon.stop()

    return pystray.Menu(
        pystray.MenuItem(status_text, None, enabled=False),
        pystray.MenuItem(last_scan_text, None, enabled=False),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem(lambda i: "המשך" if service.is_paused else "השהה", toggle_pause),
        pystray.MenuItem("סרוק עכשיו", scan_now),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("הגדרות…", open_settings),
        pystray.MenuItem("פתח לוג", lambda i, it: _open_log()),
        pystray.MenuItem("פתח תיקיית נתונים", lambda i, it: _open_data_dir()),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem(f"{APP_NAME} v{__version__}", None, enabled=False),
        pystray.MenuItem("יציאה", quit_app),
    )


def _refresh_loop(icon: pystray.Icon, stop: threading.Event) -> None:
    """Periodically refresh menu so dynamic labels (last scan time) update."""
    while not stop.wait(timeout=10):
        try:
            icon.update_menu()
        except Exception:
            pass


def main() -> int:
    # Settings re-launch path (when run from frozen exe with --settings flag).
    if "--settings" in sys.argv:
        from .settings_window import main as settings_main
        settings_main()
        return 0

    _configure_logging()
    log.info(f"{APP_NAME} v{__version__} starting")

    cfg = Config.load()
    if not cfg.is_configured():
        log.info("not configured — launching settings window first")
        # Block until settings are saved (or user quits).
        if getattr(sys, "frozen", False):
            subprocess.run([sys.executable, "--settings"])
        else:
            subprocess.run([sys.executable, "-m", "src.settings_window"], cwd=Path(__file__).resolve().parent.parent)
        cfg = Config.load()
        if not cfg.is_configured():
            log.info("still not configured — exiting")
            return 1

    db.init_db()
    service = ScannerService(cfg)
    service.start()

    icon = pystray.Icon(APP_NAME, make_icon(active=True), APP_NAME)
    icon.menu = build_menu(icon, service)

    refresh_stop = threading.Event()
    refresh_thread = threading.Thread(target=_refresh_loop, args=(icon, refresh_stop), daemon=True)
    refresh_thread.start()

    try:
        icon.run()  # blocks until quit
    finally:
        refresh_stop.set()
        service.stop()
    log.info("exited")
    return 0


if __name__ == "__main__":
    sys.exit(main())
