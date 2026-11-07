"""Uploader daemon: local archive -> NAS with rsync over SSH (through Tailscale).

Queue order:
  1. events the user asked to upload now
  2. trigger-window files (event.json, thumb.png and the minutes around the trigger, all cameras)
     before the rest of any event, so a data cap is spent on the useful part first
  3. SavedClips before SentryClips
  4. newest events first

rsync is resumable (--partial-dir) and idempotent, so a failed batch is simply retried.
The transfer is stopped if the user pauses uploads or the monthly data budget runs out.
"""

from __future__ import annotations

import logging
import sqlite3
import subprocess
import time
from pathlib import Path
from typing import List, Optional, Tuple

from . import db, meter
from .config import Config

log = logging.getLogger(__name__)

PENDING = """
SELECT f.event_id, f.name, f.size, f.priority, f.attempts
FROM files f JOIN events e ON e.id = f.event_id
WHERE f.uploaded_at IS NULL AND e.state = 'present'
  AND e.last_change <= :settled AND f.attempts < :max_attempts
"""
# rsync exit codes that mean the connection failed, not the transfer itself:
# 10 socket I/O, 12 protocol stream broken, 30 I/O timeout, 35 connect timeout, 255 ssh failure.
NETWORK_EXIT_CODES = frozenset({10, 12, 30, 35, 255})

ORDER = " ORDER BY e.upload_requested DESC, f.priority ASC, (e.type = 'SavedClips') DESC," \
        " e.ts DESC, f.start_ts ASC, f.name ASC"


def next_batch(conn: sqlite3.Connection, cfg: Config, now: Optional[float] = None) -> List[sqlite3.Row]:
    params = {
        "settled": (now or time.time()) - float(cfg.get("car.settle_seconds")),
        "max_attempts": int(cfg.get("upload.max_attempts")),
    }
    first = conn.execute(PENDING + ORDER + " LIMIT 1", params).fetchone()
    if first is None:
        return []
    params.update(event_id=first["event_id"], priority=first["priority"],
                  limit=int(cfg.get("upload.max_batch_files")))
    return conn.execute(
        PENDING + " AND f.event_id = :event_id AND f.priority = :priority" + ORDER + " LIMIT :limit",
        params,
    ).fetchall()


def fit_budget(batch: List[sqlite3.Row], remaining: int, overhead: float) -> List[sqlite3.Row]:
    out, total = [], 0.0
    for row in batch:
        total += row["size"] * overhead
        if total > remaining:
            break
        out.append(row)
    return out


def pending_summary(conn: sqlite3.Connection) -> dict:
    row = conn.execute(
        "SELECT COUNT(*) AS files, COALESCE(SUM(f.size), 0) AS bytes,"
        " COUNT(DISTINCT f.event_id) AS events"
        " FROM files f JOIN events e ON e.id = f.event_id"
        " WHERE f.uploaded_at IS NULL AND e.state = 'present'"
    ).fetchone()
    return dict(row)


# --- transport -------------------------------------------------------------------------------

def _ssh_cmd(cfg: Config) -> List[str]:
    cmd = ["ssh", "-p", str(cfg.get("upload.port")), "-o", "BatchMode=yes",
           "-o", "StrictHostKeyChecking=accept-new", "-o", "ConnectTimeout=20",
           "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=4"]
    if cfg.get("upload.ssh_key"):
        cmd += ["-i", cfg.get("upload.ssh_key")]
    return cmd


def destination(cfg: Config) -> str:
    remote_dir = str(cfg.get("upload.remote_dir")).rstrip("/") + "/"
    host = cfg.get("upload.host")
    if not host:  # local path, e.g. a mounted share or for testing
        return remote_dir
    user = cfg.get("upload.user")
    return f"{user}@{host}:{remote_dir}" if user else f"{host}:{remote_dir}"


def rsync_command(conn: sqlite3.Connection, cfg: Config, batch: List[sqlite3.Row]) -> List[str]:
    cmd = ["rsync", "-rt", "--relative", "--partial-dir=.rsync-partial", "--timeout=120"]
    bw = int(db.setting(conn, cfg, "upload.bwlimit_kbps") or 0)
    if bw:
        cmd.append(f"--bwlimit={bw}")
    if cfg.get("upload.host"):
        cmd += ["-e", " ".join(_ssh_cmd(cfg))]
    # Run from the archive dir with --relative so the NAS gets <type>/<folder>/<file>.
    cmd += [f"{row['event_id']}/{row['name']}" for row in batch]
    cmd.append(destination(cfg))
    return cmd


def check_reachable(cfg: Config) -> Tuple[bool, str]:
    host = cfg.get("upload.host")
    if not host:
        ok = Path(cfg.get("upload.remote_dir")).is_dir()
        return ok, "" if ok else "destination directory missing"
    # List the target folder with rsync itself rather than `ssh host true`: NAS accounts that are
    # allowed rsync but no login shell (e.g. a non-admin Synology user) pass this check too.
    cmd = ["rsync", "--list-only", "--timeout=30", "-e", " ".join(_ssh_cmd(cfg)), destination(cfg)]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    except subprocess.TimeoutExpired:
        return False, "NAS check timed out"
    return res.returncode == 0, res.stderr.strip()[-300:]


# --- daemon ----------------------------------------------------------------------------------

class Uploader:
    def __init__(self, conn: sqlite3.Connection, cfg: Config):
        self.conn = conn
        self.cfg = cfg
        self.metered = meter.available(cfg)
        self.failures = 0
        self.reachable_until = 0.0
        self.next_idle_check = 0.0
        self.idle_failures = 0

    def state(self, state: str, **extra) -> None:
        db.set_status(self.conn, "uploader", {"state": state, "at": time.time(), **extra})

    def sample(self) -> None:
        if meter.sample(self.conn, self.cfg) is not None:
            self.metered = True

    def paused(self) -> bool:
        return bool(db.setting(self.conn, self.cfg, "upload.paused"))

    def step(self) -> float:
        """One iteration. Returns seconds to wait before the next."""
        poll = float(self.cfg.get("upload.poll_interval"))
        self.sample()
        db.take_request(self.conn, "upload")  # wake-up flag only

        if not self.cfg.get("upload.enabled"):
            self.state("disabled")
            return poll
        if self.paused():
            self.state("paused")
            return poll

        batch = next_batch(self.conn, self.cfg)
        if not batch:
            self.state("idle")
            self.idle_check()
            return poll

        overhead = float(self.cfg.get("budget.overhead"))
        remaining = meter.remaining_bytes(self.conn, self.cfg)
        batch = fit_budget(batch, remaining, overhead)
        if not batch:
            self.state("budget", remaining=remaining)
            return poll * 5

        if time.time() > self.reachable_until:
            ok, err = self.check_nas()
            if not ok:
                self.state("offline", error=err)
                return poll

        return self.upload(batch)

    def check_nas(self) -> Tuple[bool, str]:
        """Probe the NAS and record the result for the UI. A success is trusted for 10 minutes."""
        ok, err = check_reachable(self.cfg)
        now = time.time()
        db.set_status(self.conn, "nas", {"reachable": ok, "error": err, "at": now})
        self.reachable_until = now + 600 if ok else 0.0
        if not ok:
            log.info("NAS not reachable: %s", err or "unknown error")
        return ok, err

    def idle_check(self) -> None:
        """With nothing to upload, still probe the NAS now and then so the UI shows a broken link
        (Tailscale down, NAS off, key removed) before the first clip needs it. Each probe is one
        small SSH login (a few KB over tailscale0, counted in the budget)."""
        minutes = float(self.cfg.get("upload.idle_check_minutes") or 0)
        if minutes <= 0 or time.time() < self.next_idle_check:
            return
        ok, _ = self.check_nas()
        if ok:
            self.idle_failures = 0
            wait = minutes * 60
        else:
            # Often just a race after boot (Tailscale not connected yet): retry after 1, 2, 4, ... min.
            self.idle_failures += 1
            wait = min(60 * 2 ** (self.idle_failures - 1), minutes * 60)
        self.next_idle_check = time.time() + wait

    def upload(self, batch: List[sqlite3.Row]) -> float:
        event_id = batch[0]["event_id"]
        size = sum(r["size"] for r in batch)
        self.state("uploading", event=event_id, files=len(batch), bytes=size, started=time.time())
        log.info("uploading %d files (%.1f MB) of %s", len(batch), size / 1e6, event_id)

        cmd = rsync_command(self.conn, self.cfg, batch)
        proc = subprocess.Popen(cmd, cwd=self.cfg.archive_dir, stdout=subprocess.DEVNULL,
                                stderr=subprocess.PIPE, text=True)
        stopped = ""
        while True:
            try:
                proc.wait(timeout=5)
                break
            except subprocess.TimeoutExpired:
                pass
            self.sample()
            if self.paused():
                stopped = "paused"
            elif self.metered and meter.remaining_bytes(self.conn, self.cfg) <= 0:
                stopped = "budget"
            if stopped:
                log.info("stopping transfer: %s", stopped)
                proc.terminate()
                try:
                    proc.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    proc.kill()
                break
        _, stderr = proc.communicate()

        if stopped:
            return 1
        if proc.returncode == 0:
            now = time.time()
            with db.transaction(self.conn):
                for r in batch:
                    self.conn.execute(
                        "UPDATE files SET uploaded_at=?, last_error='' WHERE event_id=? AND name=?",
                        (now, r["event_id"], r["name"]),
                    )
                remaining = self.conn.execute(
                    "SELECT COUNT(*) FROM files WHERE event_id=? AND uploaded_at IS NULL", (event_id,)
                ).fetchone()[0]
                if remaining == 0:
                    self.conn.execute("UPDATE events SET upload_requested=0 WHERE id=?", (event_id,))
            if not self.metered:
                meter.add_usage(self.conn, self.cfg, int(size * float(self.cfg.get("budget.overhead"))))
            self.failures = 0
            db.set_status(self.conn, "last_upload", {"at": now, "event": event_id, "files": len(batch), "bytes": size})
            return 0

        return self.failed(batch, proc.returncode, stderr)

    def failed(self, batch: List[sqlite3.Row], returncode: int, stderr: str) -> float:
        error = (stderr or "").strip()[-500:] or f"rsync exited {returncode}"
        network = returncode in NETWORK_EXIT_CODES
        log.warning("upload failed (%s): %s", "network" if network else "error", error)
        # Connection problems say nothing about the files: retry them indefinitely.
        # Only real errors (permissions, NAS disk full, ...) count toward max_attempts.
        bump = 0 if network else 1
        with db.transaction(self.conn):
            for r in batch:
                self.conn.execute(
                    "UPDATE files SET attempts=attempts+?, last_error=? WHERE event_id=? AND name=?",
                    (bump, error, r["event_id"], r["name"]),
                )
        self.failures += 1
        self.reachable_until = 0
        if network:
            db.set_status(self.conn, "nas", {"reachable": False, "error": error, "at": time.time()})
        self.state("offline" if network else "error", error=error)
        return min(30 * 2 ** self.failures, 900)


def run(cfg: Config) -> None:
    conn = db.open_db(cfg)
    up = Uploader(conn, cfg)
    log.info("uploader started -> %s", destination(cfg))
    while True:
        try:
            wait = up.step()
        except Exception:
            log.exception("uploader step failed")
            wait = 60
        db.set_status(conn, "uploader_heartbeat", time.time())
        deadline = time.time() + wait
        while time.time() < deadline:
            time.sleep(min(3, max(0.0, deadline - time.time())))
            if db.kv_get(conn, "request:upload"):
                break
