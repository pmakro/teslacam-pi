"""Local archive layout: <archive_dir>/<SentryClips|SavedClips>/<folder>/<files>."""

from __future__ import annotations

import shutil
import sqlite3
import time
from pathlib import Path

from .config import Config


def event_dir(cfg: Config, event_id: str) -> Path:
    path = (cfg.archive_dir / event_id).resolve()
    if cfg.archive_dir.resolve() not in path.parents:
        raise ValueError(f"invalid event id {event_id!r}")
    return path


def free_pct(path: Path) -> float:
    u = shutil.disk_usage(path)
    return 100.0 * u.free / u.total if u.total else 0.0


def remove_event_files(conn: sqlite3.Connection, cfg: Config, event_id: str) -> None:
    """Delete an event's local files. The event row stays as a tombstone so the
    archiver does not re-import it from the car drive."""
    d = event_dir(cfg, event_id)
    if d.exists():
        shutil.rmtree(d)
    conn.execute("UPDATE events SET removed_at=? WHERE id=?", (time.time(), event_id))


def purge_removed(conn: sqlite3.Connection, cfg: Config) -> int:
    """Physically delete files of events marked deleted/pruned but not yet removed."""
    rows = conn.execute(
        "SELECT id FROM events WHERE state != 'present' AND removed_at IS NULL"
    ).fetchall()
    for row in rows:
        remove_event_files(conn, cfg, row["id"])
    return len(rows)
