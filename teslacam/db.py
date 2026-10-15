"""SQLite storage shared by the archiver, uploader and web UI.

All internal times are UNIX epoch seconds (the Pi has no RTC, NTP corrects it once online);
car timestamps (event time, clip start) are naive local-time ISO strings as the car writes them.
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any, Optional

from . import config as config_mod

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id               TEXT PRIMARY KEY,          -- "SentryClips/2026-10-01_14-03-22"
    type             TEXT NOT NULL,             -- SentryClips | SavedClips
    folder           TEXT NOT NULL,
    ts               TEXT NOT NULL,             -- trigger time (event.json) or folder time
    city             TEXT NOT NULL DEFAULT '',
    reason           TEXT NOT NULL DEFAULT '',
    lat              REAL,
    lon              REAL,
    trigger_camera   TEXT NOT NULL DEFAULT '',
    state            TEXT NOT NULL DEFAULT 'present',  -- present | deleted (by user) | pruned (retention)
    protected        INTEGER NOT NULL DEFAULT 0,
    upload_requested INTEGER NOT NULL DEFAULT 0,
    first_seen       REAL NOT NULL,
    last_change      REAL NOT NULL,
    removed_at       REAL
);
CREATE INDEX IF NOT EXISTS events_state_ts ON events(state, ts);

CREATE TABLE IF NOT EXISTS files (
    event_id    TEXT NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    name        TEXT NOT NULL,
    kind        TEXT NOT NULL,                  -- video | meta
    camera      TEXT NOT NULL DEFAULT '',
    start_ts    TEXT NOT NULL DEFAULT '',
    size        INTEGER NOT NULL,
    priority    INTEGER NOT NULL,               -- 0 trigger window + metadata, 1 remainder
    archived_at REAL NOT NULL,
    uploaded_at REAL,
    attempts    INTEGER NOT NULL DEFAULT 0,
    last_error  TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (event_id, name)
);
CREATE INDEX IF NOT EXISTS files_pending ON files(uploaded_at, priority);

CREATE TABLE IF NOT EXISTS usage (
    period TEXT PRIMARY KEY,                    -- billing period start, "2026-10-01"
    bytes  INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS kv (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at REAL NOT NULL
);
"""


def connect(path: Path | str) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), timeout=30, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def init(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.execute("PRAGMA user_version=%d" % SCHEMA_VERSION)


def open_db(cfg: config_mod.Config) -> sqlite3.Connection:
    cfg.db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = connect(cfg.db_path)
    init(conn)
    return conn


class transaction:
    """`with transaction(conn):` - BEGIN IMMEDIATE / COMMIT / ROLLBACK (autocommit connections)."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def __enter__(self):
        self.conn.execute("BEGIN IMMEDIATE")
        return self.conn

    def __exit__(self, exc_type, exc, tb):
        self.conn.execute("ROLLBACK" if exc_type else "COMMIT")
        return False


# --- key/value: runtime settings ("setting:<key>") and daemon status ("status:<key>") ---

def kv_get(conn: sqlite3.Connection, key: str, default: Any = None) -> Any:
    row = conn.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
    return json.loads(row["value"]) if row else default


def kv_set(conn: sqlite3.Connection, key: str, value: Any) -> None:
    conn.execute(
        "INSERT INTO kv(key, value, updated_at) VALUES(?,?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
        (key, json.dumps(value), time.time()),
    )


def kv_pop(conn: sqlite3.Connection, key: str) -> Any:
    with transaction(conn):
        value = kv_get(conn, key)
        conn.execute("DELETE FROM kv WHERE key=?", (key,))
    return value


def setting(conn: sqlite3.Connection, cfg: config_mod.Config, key: str) -> Any:
    """Runtime value of a setting: DB override if present, else config file."""
    if key in config_mod.RUNTIME_SETTINGS:
        row = conn.execute("SELECT value FROM kv WHERE key=?", ("setting:" + key,)).fetchone()
        if row:
            return json.loads(row["value"])
    return cfg.get(key)


def set_setting(conn: sqlite3.Connection, key: str, raw: Any) -> None:
    kv_set(conn, "setting:" + key, config_mod.coerce_setting(key, raw))


def status(conn: sqlite3.Connection, key: str, default: Any = None) -> Any:
    return kv_get(conn, "status:" + key, default)


def set_status(conn: sqlite3.Connection, key: str, value: Any) -> None:
    kv_set(conn, "status:" + key, value)


# --- requests from the (unprivileged) web UI to the daemons ---

def request(conn: sqlite3.Connection, name: str, value: Any = True) -> None:
    kv_set(conn, "request:" + name, value)


def take_request(conn: sqlite3.Connection, name: str) -> Optional[Any]:
    return kv_pop(conn, "request:" + name)
