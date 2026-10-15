"""Archiver daemon: car drive -> local archive, plus car-drive cleanup and local retention.

Each cycle:
  1. handle UI requests (eject / attach / cleanup now)
  2. snapshot the car drive and copy new or changed Sentry/Saved files into the archive
  3. physically remove files of events deleted in the UI
  4. apply local retention
  5. if the car drive is getting full (and idle), delete already-archived events from it
"""

from __future__ import annotations

import logging
import os
import shutil
import sqlite3
import time
from pathlib import Path
from typing import List, Optional

from . import db, retention, storage, tesla
from .car import CarDrive, CarDriveError
from .config import Config

log = logging.getLogger(__name__)

# Never fill the archive past this, even with nothing prunable.
HARD_FLOOR_BYTES = 512 * 1024 * 1024
# Minimum time between automatic car-drive cleanups.
CLEANUP_COOLDOWN = 15 * 60


class ArchiveFull(Exception):
    pass


def _copy_file(src: Path, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name("." + dest.name + ".part")
    shutil.copyfile(src, tmp)
    shutil.copystat(src, tmp)
    os.replace(tmp, dest)


def scan(conn: sqlite3.Connection, cfg: Config, root: Path) -> dict:
    """Copy new/changed event files from a TeslaCam/ root. Idempotent."""
    stats = {"events_new": 0, "files_copied": 0, "bytes_copied": 0}
    now = time.time()

    for event_type, folder in tesla.iter_event_dirs(root):
        event_id = f"{event_type}/{folder.name}"
        row = conn.execute("SELECT state FROM events WHERE id=?", (event_id,)).fetchone()
        if row and row["state"] != "present":
            continue  # deleted/pruned: tombstone, never re-import

        meta = tesla.read_event_json(folder / "event.json")
        trigger = meta.timestamp or tesla.parse_folder_ts(folder.name)
        dest_dir = storage.event_dir(cfg, event_id)

        try:
            entries = sorted(p for p in folder.iterdir() if p.is_file() and tesla.is_event_file(p.name))
        except OSError as e:
            log.warning("cannot list %s: %s", folder, e)
            continue

        if row is None:
            conn.execute(
                "INSERT INTO events(id, type, folder, ts, city, reason, lat, lon, trigger_camera,"
                " first_seen, last_change) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (event_id, event_type, folder.name, trigger.isoformat(), meta.city, meta.reason,
                 meta.lat, meta.lon, meta.camera, now, now),
            )
            stats["events_new"] += 1
            log.info("new event %s (%s)", event_id, meta.reason or "no reason")

        changed = False
        for src in entries:
            try:
                size = src.stat().st_size
            except OSError:
                continue
            if size == 0:
                continue  # car has created but not yet written the file
            known = conn.execute(
                "SELECT size FROM files WHERE event_id=? AND name=?", (event_id, src.name)
            ).fetchone()
            dest = dest_dir / src.name
            if known and known["size"] == size and dest.exists() and dest.stat().st_size == size:
                continue

            if shutil.disk_usage(cfg.archive_dir).free - size < HARD_FLOOR_BYTES:
                raise ArchiveFull(f"archive full, cannot copy {event_id}/{src.name}")

            try:
                _copy_file(src, dest)
            except OSError as e:
                log.warning("copy failed %s: %s", src, e)
                continue

            clip = tesla.parse_clip_name(src.name)
            conn.execute(
                "INSERT INTO files(event_id, name, kind, camera, start_ts, size, priority, archived_at)"
                " VALUES(?,?,?,?,?,?,?,?)"
                " ON CONFLICT(event_id, name) DO UPDATE SET size=excluded.size,"
                " archived_at=excluded.archived_at, uploaded_at=NULL, attempts=0, last_error=''",
                (event_id, src.name, "video" if clip else "meta",
                 clip.camera if clip else "", clip.start.isoformat() if clip else "",
                 size, tesla.file_priority(src.name, trigger), now),
            )
            changed = True
            stats["files_copied"] += 1
            stats["bytes_copied"] += size

        if changed:
            conn.execute(
                "UPDATE events SET last_change=?, ts=?, city=?, reason=?, lat=?, lon=?, trigger_camera=?"
                " WHERE id=?",
                (now, trigger.isoformat(), meta.city, meta.reason, meta.lat, meta.lon, meta.camera, event_id),
            )
    return stats


def _removable(conn: sqlite3.Connection, cfg: Config, event_id: str, folder: Path, now: float) -> bool:
    """Can this event folder be deleted from the car drive? Yes if the user deleted it, or if it
    stopped changing and every file is archived with the same size."""
    row = conn.execute("SELECT state, last_change FROM events WHERE id=?", (event_id,)).fetchone()
    if row is None:
        return False
    if row["state"] != "present":
        return True
    if now - row["last_change"] < float(cfg.get("car.settle_seconds")):
        return False
    return _fully_archived(conn, event_id, folder)


def cleanup_candidates(conn: sqlite3.Connection, cfg: Config, root: Path) -> List[str]:
    now = time.time()
    return [f"{t}/{folder.name}" for t, folder in tesla.iter_event_dirs(root)
            if _removable(conn, cfg, f"{t}/{folder.name}", folder, now)]


def car_cleanup(conn: sqlite3.Connection, cfg: Config, car: CarDrive) -> dict:
    """Delete events from the car drive that are safely archived (or deleted in the UI)."""
    freed = {"events": 0, "bytes": 0}
    now = time.time()
    with car.read_write() as root:
        for event_type, folder in tesla.iter_event_dirs(root):
            if not _removable(conn, cfg, f"{event_type}/{folder.name}", folder, now):
                continue
            size = sum(p.stat().st_size for p in folder.iterdir() if p.is_file())
            shutil.rmtree(folder, ignore_errors=True)
            freed["events"] += 1
            freed["bytes"] += size
    log.info("car cleanup freed %d events, %.1f MB", freed["events"], freed["bytes"] / 1e6)
    return freed


def _fully_archived(conn: sqlite3.Connection, event_id: str, folder: Path) -> bool:
    known = {r["name"]: r["size"] for r in conn.execute(
        "SELECT name, size FROM files WHERE event_id=?", (event_id,))}
    for p in folder.iterdir():
        if p.is_file() and tesla.is_event_file(p.name):
            size = p.stat().st_size
            if size and known.get(p.name) != size:
                return False
    return True


def _handle_requests(conn: sqlite3.Connection, car: CarDrive) -> bool:
    """Returns True if a manual cleanup was requested."""
    if db.take_request(conn, "eject"):
        log.info("ejecting car drive (UI request)")
        car.eject()
    if db.take_request(conn, "attach"):
        log.info("attaching car drive (UI request)")
        car.attach()
    return bool(db.take_request(conn, "cleanup"))


def cycle(conn: sqlite3.Connection, cfg: Config, car: CarDrive) -> None:
    force_cleanup = False
    try:
        force_cleanup = _handle_requests(conn, car)
    except CarDriveError as e:
        log.error("request failed: %s", e)
        db.set_status(conn, "archiver_error", str(e))

    cfg.archive_dir.mkdir(parents=True, exist_ok=True)
    car_free_pct: Optional[float] = None
    candidates: List[str] = []
    error = ""
    try:
        with car.snapshot() as root:
            if root is not None:
                stats = scan(conn, cfg, root)
                total, free = car.usage(root)
                car_free_pct = 100.0 * free / total if total else None
                db.set_status(conn, "car_usage", {"total": total, "free": free})
                candidates = cleanup_candidates(conn, cfg, root)
                if stats["files_copied"]:
                    log.info("archived %(files_copied)d files (%(bytes_copied)d bytes), %(events_new)d new events", stats)
            db.set_status(conn, "car_present", root is not None)
    except ArchiveFull as e:
        error = str(e)
        log.warning(error)
    except CarDriveError as e:
        error = f"car drive: {e}"
        log.error(error)

    storage.purge_removed(conn, cfg)
    retention.run(conn, cfg)

    # Detaching the drive interrupts the car, so only do it when it will actually free something,
    # and (unless asked from the UI) not more often than CLEANUP_COOLDOWN.
    threshold = float(cfg.get("car.cleanup_free_pct"))
    needs_cleanup = car_free_pct is not None and car_free_pct < threshold
    last = (db.status(conn, "last_cleanup") or {}).get("at", 0)
    if force_cleanup and not candidates:
        log.info("cleanup requested, but nothing on the car drive is archived yet")
    elif candidates and (force_cleanup or (needs_cleanup and time.time() - last >= CLEANUP_COOLDOWN)):
        idle = car.idle_seconds()
        if force_cleanup or idle >= float(cfg.get("car.idle_seconds")):
            try:
                freed = car_cleanup(conn, cfg, car)
                db.set_status(conn, "last_cleanup", {"at": time.time(), **freed})
            except CarDriveError as e:
                error = f"cleanup: {e}"
                log.error(error)
        else:
            log.info("car drive %.0f%% free, waiting for idle (%.0fs)", car_free_pct, idle)

    if cfg.archive_dir.exists():
        u = shutil.disk_usage(cfg.archive_dir)
        db.set_status(conn, "archive_usage", {"total": u.total, "free": u.free})
    try:
        db.set_status(conn, "car_attached", car.is_attached())
    except CarDriveError:
        pass
    db.set_status(conn, "archiver_error", error)
    db.set_status(conn, "archiver_heartbeat", time.time())


def run(cfg: Config) -> None:
    from .car import from_config

    conn = db.open_db(cfg)
    car = from_config(cfg)
    interval = float(cfg.get("car.scan_interval"))
    log.info("archiver started (mode=%s, interval=%ss)", cfg.get("car.mode"), interval)
    while True:
        started = time.time()
        try:
            cycle(conn, cfg, car)
        except Exception:
            log.exception("archiver cycle failed")
        # Sleep until the next cycle, waking early for UI requests.
        while time.time() - started < interval:
            time.sleep(3)
            if conn.execute("SELECT 1 FROM kv WHERE key IN ('request:eject', 'request:attach', 'request:cleanup')").fetchone():
                break
