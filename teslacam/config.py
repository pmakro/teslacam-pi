"""Configuration: TOML file on disk, with a few values overridable at runtime from the UI."""

from __future__ import annotations

import copy
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict

try:  # Python 3.11+ (Raspberry Pi OS Bookworm)
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - older dev machines
    import tomli as tomllib

DEFAULT_PATH = "/etc/teslacam/config.toml"

DEFAULTS: Dict[str, Dict[str, Any]] = {
    "paths": {
        "archive_dir": "/archive/events",
        "db_path": "/archive/teslacam.db",
    },
    "car": {
        # "image": Pi mode, the car writes into a disk image exposed by the USB gadget.
        # "directory": dev mode, source_dir already contains a TeslaCam/ folder.
        "mode": "image",
        "source_dir": "",
        "image_path": "/backingfiles/cam_disk.bin",
        "snapshot_mount": "/mnt/teslacam/snap",
        "rw_mount": "/mnt/teslacam/rw",
        "priv_helper": "/opt/teslacam/scripts/priv.sh",
        "scan_interval": 120,
        "settle_seconds": 300,
        "cleanup_free_pct": 25,
        "idle_seconds": 90,
    },
    "upload": {
        "enabled": True,
        "paused": False,
        "host": "nas",
        "user": "teslacam",
        "port": 22,
        "remote_dir": "/volume1/TeslaCam",
        "ssh_key": "",
        "bwlimit_kbps": 0,
        "max_batch_files": 12,
        "max_attempts": 5,
        "poll_interval": 60,
    },
    "budget": {
        "monthly_gb": 20.0,
        "billing_day": 1,
        "interface": "tailscale0",
        "overhead": 1.06,
    },
    "retention": {
        "days": 14,
        "min_free_pct": 15,
    },
    "network": {
        "watchdog": True,
        "interface": "wlan0",
        "stuck_minutes": 10,
        "check_interval": 30,
        "setup_hotspot": True,
        "setup_ssid": "TeslaCam-Setup",
        "setup_password": "",
        "setup_address": "10.42.0.1",
        "fallback_minutes": 3,
        "setup_minutes": 5,
    },
    "web": {
        "host": "0.0.0.0",
        "port": 80,
        "password_hash": "",
        "secret_key": "",
    },
}

# Settings the UI may change at runtime. Stored in the DB, they take precedence over the file.
RUNTIME_SETTINGS = {
    "upload.paused": bool,
    "upload.bwlimit_kbps": int,
    "budget.monthly_gb": float,
    "budget.billing_day": int,
    "retention.days": int,
    "retention.min_free_pct": int,
}


@dataclass
class Config:
    data: Dict[str, Dict[str, Any]] = field(default_factory=lambda: copy.deepcopy(DEFAULTS))
    path: str = ""

    def get(self, dotted: str) -> Any:
        section, key = dotted.split(".", 1)
        return self.data.get(section, {}).get(key)

    def section(self, name: str) -> Dict[str, Any]:
        return self.data[name]

    @property
    def archive_dir(self) -> Path:
        return Path(self.get("paths.archive_dir"))

    @property
    def db_path(self) -> Path:
        return Path(self.get("paths.db_path"))


def load(path: str | None = None) -> Config:
    path = path or os.environ.get("TESLACAM_CONFIG") or DEFAULT_PATH
    data = copy.deepcopy(DEFAULTS)
    if os.path.exists(path):
        with open(path, "rb") as f:
            user = tomllib.load(f)
        for section, values in user.items():
            if section not in data:
                raise ValueError(f"unknown config section [{section}] in {path}")
            for key, value in values.items():
                if key not in data[section]:
                    raise ValueError(f"unknown config key {section}.{key} in {path}")
                data[section][key] = value
    cfg = Config(data=data, path=path)
    _validate(cfg)
    return cfg


def _validate(cfg: Config) -> None:
    mode = cfg.get("car.mode")
    if mode not in ("image", "directory"):
        raise ValueError("car.mode must be 'image' or 'directory'")
    if mode == "directory" and not cfg.get("car.source_dir"):
        raise ValueError("car.source_dir is required when car.mode = 'directory'")
    day = int(cfg.get("budget.billing_day"))
    if not 1 <= day <= 28:
        raise ValueError("budget.billing_day must be between 1 and 28")


def coerce_setting(key: str, raw: Any) -> Any:
    kind = RUNTIME_SETTINGS[key]
    if kind is bool:
        if isinstance(raw, str):
            return raw.lower() in ("1", "true", "on", "yes")
        return bool(raw)
    value = kind(raw)
    if key == "budget.billing_day" and not 1 <= value <= 28:
        raise ValueError("billing day must be between 1 and 28")
    if value < 0:
        raise ValueError(f"{key} must not be negative")
    return value
