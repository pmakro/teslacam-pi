"""Mobile data accounting against a monthly budget.

Usage is measured from the byte counters of the interface uploads go through (tailscale0 by
default, so UI traffic on the hotspot LAN is not counted) times an overhead factor for the
WireGuard/LTE framing. Counters reset on reboot, so deltas are persisted per billing period.
When the interface has no counters (e.g. on a dev machine) the uploader falls back to adding
the size of uploaded files.
"""

from __future__ import annotations

import sqlite3
from datetime import date, datetime
from pathlib import Path
from typing import Optional

from . import db
from .config import Config

BOOT_ID = Path("/proc/sys/kernel/random/boot_id")


def period_start(now: datetime, billing_day: int) -> date:
    if now.day >= billing_day:
        return date(now.year, now.month, billing_day)
    if now.month == 1:
        return date(now.year - 1, 12, billing_day)
    return date(now.year, now.month - 1, billing_day)


def current_period(conn: sqlite3.Connection, cfg: Config, now: Optional[datetime] = None) -> str:
    day = int(db.setting(conn, cfg, "budget.billing_day"))
    return period_start(now or datetime.now(), day).isoformat()


def add_usage(conn: sqlite3.Connection, cfg: Config, nbytes: int, now: Optional[datetime] = None) -> None:
    if nbytes <= 0:
        return
    conn.execute(
        "INSERT INTO usage(period, bytes) VALUES(?, ?) "
        "ON CONFLICT(period) DO UPDATE SET bytes = bytes + excluded.bytes",
        (current_period(conn, cfg, now), int(nbytes)),
    )


def used_bytes(conn: sqlite3.Connection, cfg: Config, now: Optional[datetime] = None) -> int:
    row = conn.execute("SELECT bytes FROM usage WHERE period=?", (current_period(conn, cfg, now),)).fetchone()
    return int(row["bytes"]) if row else 0


def budget_bytes(conn: sqlite3.Connection, cfg: Config) -> int:
    return int(float(db.setting(conn, cfg, "budget.monthly_gb")) * 1e9)


def remaining_bytes(conn: sqlite3.Connection, cfg: Config) -> int:
    return budget_bytes(conn, cfg) - used_bytes(conn, cfg)


def _read_counters(interface: str, sysfs: Path) -> Optional[int]:
    base = sysfs / interface / "statistics"
    try:
        return int((base / "rx_bytes").read_text()) + int((base / "tx_bytes").read_text())
    except (OSError, ValueError):
        return None


def _boot_id() -> str:
    try:
        return BOOT_ID.read_text().strip()
    except OSError:
        return ""


def available(cfg: Config, sysfs: Path = Path("/sys/class/net")) -> bool:
    return _read_counters(cfg.get("budget.interface"), sysfs) is not None


def sample(conn: sqlite3.Connection, cfg: Config, sysfs: Path = Path("/sys/class/net"),
           boot_id: Optional[str] = None) -> Optional[int]:
    """Add interface traffic since the last sample to the current period. Returns bytes added,
    or None if the interface has no counters (down, or not Linux)."""
    counter = _read_counters(cfg.get("budget.interface"), sysfs)
    if counter is None:
        return None
    boot = boot_id if boot_id is not None else _boot_id()
    last = db.kv_get(conn, "meter:last")
    if last and last.get("boot") == boot and last.get("iface") == cfg.get("budget.interface") \
            and counter >= last.get("counter", 0):
        delta = counter - last["counter"]
    else:
        # New boot or interface re-created: counters restarted from zero.
        delta = counter if last else 0
    added = int(delta * float(cfg.get("budget.overhead")))
    add_usage(conn, cfg, added)
    db.kv_set(conn, "meter:last", {"boot": boot, "iface": cfg.get("budget.interface"), "counter": counter})
    return added
