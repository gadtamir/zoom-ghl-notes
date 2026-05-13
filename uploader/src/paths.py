"""Platform-aware paths for config, local DB, and default Zoom recordings folder."""

import sys
from pathlib import Path

from platformdirs import user_data_dir

from . import APP_NAME


def app_data_dir() -> Path:
    """Where we store config.json and local.db."""
    d = Path(user_data_dir(APP_NAME, appauthor=False, roaming=True))
    d.mkdir(parents=True, exist_ok=True)
    return d


def config_path() -> Path:
    return app_data_dir() / "config.json"


def local_db_path() -> Path:
    return app_data_dir() / "local.db"


def log_path() -> Path:
    p = app_data_dir() / "uploader.log"
    return p


def default_zoom_folder() -> Path:
    """Zoom's default recordings folder on each OS.

    macOS: ~/Documents/Zoom
    Windows: %USERPROFILE%\\Documents\\Zoom
    Linux:  ~/Documents/Zoom (for dev)
    """
    home = Path.home()
    if sys.platform == "darwin":
        return home / "Documents" / "Zoom"
    if sys.platform == "win32":
        return home / "Documents" / "Zoom"
    return home / "Documents" / "Zoom"
