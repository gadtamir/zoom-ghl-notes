"""Persistent config for the uploader."""

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .paths import config_path, default_zoom_folder


@dataclass
class Config:
    employee_name: str = ""
    api_key: str = ""
    server_url: str = "https://zoom-ghl-server.onrender.com"
    watch_folder: str = ""
    scan_interval_minutes: int = 30
    move_after_upload: bool = True

    @classmethod
    def load(cls) -> "Config":
        path = config_path()
        if not path.exists():
            cfg = cls(watch_folder=str(default_zoom_folder()))
            cfg.save()
            return cfg
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})

    def save(self) -> None:
        path = config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as f:
            json.dump(asdict(self), f, ensure_ascii=False, indent=2)

    def is_configured(self) -> bool:
        return bool(self.employee_name and self.api_key and self.server_url and self.watch_folder)
