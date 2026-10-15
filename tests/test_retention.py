import time
from datetime import datetime

from teslacam import archiver, retention, storage

from .conftest import make_event


def _setup(env):
    for day in (1, 2, 3):
        make_event(env.teslacam, "SentryClips", datetime(2026, 10, day, 10, 0, 0), minutes=1)
    archiver.scan(env.conn, env.cfg, env.teslacam)


def _states(env):
    return {r["id"][-19:-9]: r["state"] for r in env.conn.execute("SELECT id, state FROM events")}


def test_never_prunes_unuploaded_or_protected(env, monkeypatch):
    _setup(env)
    env.cfg.data["retention"]["days"] = 0
    env.conn.execute("UPDATE files SET uploaded_at=1 WHERE event_id LIKE '%10-01_%' OR event_id LIKE '%10-02_%'")
    env.conn.execute("UPDATE events SET protected=1 WHERE id LIKE '%10-02_%'")
    assert retention.run(env.conn, env.cfg) == 1
    assert _states(env) == {"2026-10-01": "pruned", "2026-10-02": "present", "2026-10-03": "present"}
    assert not (env.cfg.archive_dir / "SentryClips" / "2026-10-01_10-00-00").exists()


def test_age_threshold(env):
    _setup(env)
    env.cfg.data["retention"]["days"] = 7
    env.conn.execute("UPDATE files SET uploaded_at=1")
    assert retention.run(env.conn, env.cfg) == 0
    env.conn.execute("UPDATE events SET first_seen=? WHERE id LIKE '%10-01_%'", (time.time() - 8 * 86400,))
    assert retention.run(env.conn, env.cfg) == 1


def test_low_space_prunes_oldest_first(env, monkeypatch):
    _setup(env)
    env.cfg.data["retention"]["days"] = 365
    env.cfg.data["retention"]["min_free_pct"] = 15
    env.conn.execute("UPDATE files SET uploaded_at=1")
    calls = iter([5.0, 50.0])
    monkeypatch.setattr(storage, "free_pct", lambda path: next(calls))
    assert retention.run(env.conn, env.cfg) == 1
    assert _states(env)["2026-10-01"] == "pruned"
