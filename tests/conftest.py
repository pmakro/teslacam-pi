from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from teslacam import config as config_mod
from teslacam import db

CAMS = ("front", "back", "left_repeater", "right_repeater")


def make_event(teslacam_root: Path, event_type: str, trigger: datetime, minutes: int = 10,
               cameras=CAMS, size: int = 1000, reason: str = "sentry_aware_object_detection") -> Path:
    """Write a fake Tesla event folder: `minutes` one-minute clips ending at the trigger."""
    folder = teslacam_root / event_type / trigger.strftime("%Y-%m-%d_%H-%M-%S")
    folder.mkdir(parents=True)
    start = trigger - timedelta(minutes=minutes - 1)
    for i in range(minutes):
        ts = (start + timedelta(minutes=i)).strftime("%Y-%m-%d_%H-%M-%S")
        for cam in cameras:
            (folder / f"{ts}-{cam}.mp4").write_bytes(b"\0" * size)
    (folder / "event.json").write_text(json.dumps({
        "timestamp": trigger.isoformat(), "city": "Athens", "est_lat": "37.98",
        "est_lon": "23.72", "reason": reason, "camera": "0",
    }))
    (folder / "thumb.png").write_bytes(b"\x89PNG")
    return folder


@pytest.fixture
def env(tmp_path):
    car_dir = tmp_path / "car"
    (car_dir / "TeslaCam").mkdir(parents=True)
    nas = tmp_path / "nas"
    nas.mkdir()
    cfg = config_mod.Config(path="test")
    cfg.data["paths"]["archive_dir"] = str(tmp_path / "archive" / "events")
    cfg.data["paths"]["db_path"] = str(tmp_path / "archive" / "teslacam.db")
    cfg.data["car"]["mode"] = "directory"
    cfg.data["car"]["source_dir"] = str(car_dir)
    cfg.data["car"]["settle_seconds"] = 0
    cfg.data["upload"]["host"] = ""
    cfg.data["upload"]["remote_dir"] = str(nas)
    cfg.data["budget"]["interface"] = "does-not-exist0"
    cfg.data["budget"]["overhead"] = 1.0
    cfg.data["retention"]["min_free_pct"] = 0  # independent of the test machine's disk
    cfg.archive_dir.mkdir(parents=True)
    conn = db.open_db(cfg)

    class Env:
        pass

    e = Env()
    e.cfg, e.conn, e.teslacam, e.nas, e.tmp = cfg, conn, car_dir / "TeslaCam", nas, tmp_path
    return e
