"""Local archive pruning.

Only events that are fully uploaded and not protected are ever pruned automatically:
  1. once older than `retention.days` (0 = right after upload), and
  2. oldest-first while free space is below `retention.min_free_pct`.
"""

from __future__ import annotations

import logging
import sqlite3
import time

from . import db, storage
from .config import Config

log = logging.getLogger(__name__)

PRUNABLE = """
SELECT e.id FROM events e
WHERE e.state = 'present' AND e.protected = 0
  AND EXISTS (SELECT 1 FROM files f WHERE f.event_id = e.id)
  AND NOT EXISTS (SELECT 1 FROM files f WHERE f.event_id = e.id AND f.uploaded_at IS NULL)
"""


def _prune(conn: sqlite3.Connection, cfg: Config, event_id: str) -> None:
    conn.execute("UPDATE events SET state='pruned' WHERE id=?", (event_id,))
    storage.remove_event_files(conn, cfg, event_id)
    log.info("pruned %s", event_id)


def run(conn: sqlite3.Connection, cfg: Config) -> int:
    days = float(db.setting(conn, cfg, "retention.days"))
    min_free = float(db.setting(conn, cfg, "retention.min_free_pct"))
    pruned = 0

    cutoff = time.time() - days * 86400
    for row in conn.execute(PRUNABLE + " AND e.first_seen <= ? ORDER BY e.ts", (cutoff,)).fetchall():
        _prune(conn, cfg, row["id"])
        pruned += 1

    if cfg.archive_dir.exists():
        for row in conn.execute(PRUNABLE + " ORDER BY e.ts").fetchall():
            if storage.free_pct(cfg.archive_dir) >= min_free:
                break
            _prune(conn, cfg, row["id"])
            pruned += 1
    return pruned
