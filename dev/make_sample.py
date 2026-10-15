"""Create a fake car drive (dev/data/car/TeslaCam) with a few Sentry/Saved events for local testing.

Video clips come from dev/gen_clip.swift on macOS (playable H.264). Elsewhere, pass --fake to
write placeholder bytes instead (the UI works, the player does not).
"""

from __future__ import annotations

import argparse
import json
import shutil
import struct
import subprocess
import sys
import zlib
from datetime import datetime, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
DATA = HERE / "data"
CAMS = {
    "front": (0.9, 0.25, 0.25),
    "back": (0.25, 0.55, 0.95),
    "left_repeater": (0.3, 0.8, 0.4),
    "right_repeater": (0.95, 0.75, 0.2),
}
EVENTS = [
    ("SentryClips", datetime(2026, 10, 8, 23, 41, 7), "sentry_aware_object_detection", "Athens"),
    ("SentryClips", datetime(2026, 10, 7, 13, 2, 55), "sentry_aware_accel_0.42", "Piraeus"),
    ("SavedClips", datetime(2026, 10, 6, 18, 20, 12), "user_interaction_honk", "Athens"),
    ("SentryClips", datetime(2026, 10, 5, 9, 15, 30), "sentry_aware_object_detection", "Glyfada"),
]


def png(width: int, height: int, rgb) -> bytes:
    row = b"\x00" + bytes(int(c * 255) for c in rgb) * width
    raw = row * height

    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


def clips(fake: bool) -> dict:
    out = DATA / "clips"
    out.mkdir(parents=True, exist_ok=True)
    result = {}
    for cam, rgb in CAMS.items():
        path = out / f"{cam}.mp4"
        if not path.exists():
            if fake:
                path.write_bytes(b"\0" * 200_000)
            else:
                subprocess.run(["swift", str(HERE / "gen_clip.swift"), str(path), cam.replace("_", " ").upper(),
                                *map(str, rgb), "60"], check=True, capture_output=True)
        result[cam] = path
    return result


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fake", action="store_true", help="placeholder bytes instead of real videos")
    ap.add_argument("--minutes", type=int, default=4)
    args = ap.parse_args()

    sources = clips(args.fake or sys.platform != "darwin")
    root = DATA / "car" / "TeslaCam"
    for event_type, trigger, reason, city in EVENTS:
        folder = root / event_type / trigger.strftime("%Y-%m-%d_%H-%M-%S")
        if folder.exists():
            continue
        folder.mkdir(parents=True)
        start = trigger - timedelta(minutes=args.minutes - 1)
        for i in range(args.minutes):
            ts = (start + timedelta(minutes=i)).strftime("%Y-%m-%d_%H-%M-%S")
            for cam, src in sources.items():
                shutil.copyfile(src, folder / f"{ts}-{cam}.mp4")
        (folder / "event.json").write_text(json.dumps({
            "timestamp": trigger.isoformat(), "city": city, "est_lat": "37.9838", "est_lon": "23.7275",
            "reason": reason, "camera": "0",
        }))
        (folder / "thumb.png").write_bytes(png(160, 90, CAMS["front" if event_type == "SentryClips" else "back"]))
        print("created", folder.relative_to(DATA))
    (root / "RecentClips").mkdir(exist_ok=True)
    (DATA / "nas").mkdir(exist_ok=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
