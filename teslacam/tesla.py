"""Understanding the TeslaCam folder layout written by the car.

    TeslaCam/
      SentryClips/2026-10-01_14-03-22/
        2026-10-01_13-53-10-front.mp4
        2026-10-01_13-53-10-back.mp4
        2026-10-01_13-53-10-left_repeater.mp4
        ...
        event.json   {"timestamp": "2026-10-01T14:03:22", "city": ..., "reason": ...}
        thumb.png
      SavedClips/<same structure>
      RecentClips/   (rolling buffer, ignored)

Timestamps are the car's local time without a timezone, so they are kept naive.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterator, Optional

EVENT_TYPES = ("SavedClips", "SentryClips")

CAMERAS = (
    "front",
    "back",
    "left_repeater",
    "right_repeater",
    "left_pillar",
    "right_pillar",
)

FOLDER_RE = re.compile(r"^\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}$")
CLIP_RE = re.compile(r"^(?P<ts>\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2})-(?P<camera>[a-z_]+)\.mp4$")
META_FILES = ("event.json", "thumb.png")

TS_FMT = "%Y-%m-%d_%H-%M-%S"

# Upload "trigger window": clips overlapping [trigger - BEFORE, trigger + AFTER) go first.
TRIGGER_BEFORE = timedelta(minutes=2)
TRIGGER_AFTER = timedelta(minutes=1)
CLIP_LENGTH = timedelta(minutes=1)


@dataclass
class EventMeta:
    timestamp: Optional[datetime] = None
    city: str = ""
    reason: str = ""
    lat: Optional[float] = None
    lon: Optional[float] = None
    camera: str = ""


@dataclass
class ClipName:
    start: datetime
    camera: str


def parse_folder_ts(name: str) -> Optional[datetime]:
    if not FOLDER_RE.match(name):
        return None
    try:
        return datetime.strptime(name, TS_FMT)
    except ValueError:
        return None


def parse_clip_name(name: str) -> Optional[ClipName]:
    m = CLIP_RE.match(name)
    if not m:
        return None
    try:
        start = datetime.strptime(m.group("ts"), TS_FMT)
    except ValueError:
        return None
    return ClipName(start=start, camera=m.group("camera"))


def _float(value) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def read_event_json(path: Path) -> EventMeta:
    try:
        raw = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, ValueError):
        return EventMeta()
    ts = None
    if raw.get("timestamp"):
        try:
            ts = datetime.fromisoformat(str(raw["timestamp"]))
        except ValueError:
            ts = None
    return EventMeta(
        timestamp=ts,
        city=str(raw.get("city", "") or ""),
        reason=str(raw.get("reason", "") or ""),
        lat=_float(raw.get("est_lat")),
        lon=_float(raw.get("est_lon")),
        camera=str(raw.get("camera", "") or ""),
    )


def iter_event_dirs(teslacam_root: Path) -> Iterator[tuple]:
    """Yield (event_type, folder_path) for every Sentry/Saved event under a TeslaCam/ root."""
    for event_type in EVENT_TYPES:
        base = teslacam_root / event_type
        if not base.is_dir():
            continue
        for child in sorted(base.iterdir()):
            if child.is_dir() and parse_folder_ts(child.name):
                yield event_type, child


def is_event_file(name: str) -> bool:
    return name in META_FILES or parse_clip_name(name) is not None


def in_trigger_window(clip_start: datetime, trigger: Optional[datetime]) -> bool:
    if trigger is None:
        return True
    clip_end = clip_start + CLIP_LENGTH
    return clip_end > trigger - TRIGGER_BEFORE and clip_start < trigger + TRIGGER_AFTER


def file_priority(name: str, trigger: Optional[datetime]) -> int:
    """0 = upload first (metadata + trigger window), 1 = rest of the event."""
    clip = parse_clip_name(name)
    if clip is None:
        return 0
    return 0 if in_trigger_window(clip.start, trigger) else 1
