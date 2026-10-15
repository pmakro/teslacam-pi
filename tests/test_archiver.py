from datetime import datetime

from teslacam import archiver, db, storage
from teslacam.car import DirectoryCar

from .conftest import make_event


def test_scan_copies_and_is_idempotent(env):
    make_event(env.teslacam, "SentryClips", datetime(2026, 10, 1, 14, 3, 22), minutes=3)
    (env.teslacam / "RecentClips").mkdir()
    (env.teslacam / "RecentClips" / "2026-10-01_14-00-00-front.mp4").write_bytes(b"x")

    stats = archiver.scan(env.conn, env.cfg, env.teslacam)
    assert stats["events_new"] == 1
    assert stats["files_copied"] == 3 * 4 + 2
    dest = env.cfg.archive_dir / "SentryClips" / "2026-10-01_14-03-22"
    assert (dest / "event.json").exists()
    assert not (env.cfg.archive_dir / "RecentClips").exists()

    again = archiver.scan(env.conn, env.cfg, env.teslacam)
    assert again["files_copied"] == 0

    row = env.conn.execute("SELECT * FROM events").fetchone()
    assert row["reason"] == "sentry_aware_object_detection" and row["city"] == "Athens"


def test_grown_file_is_recopied_and_reuploaded(env):
    folder = make_event(env.teslacam, "SavedClips", datetime(2026, 10, 1, 9, 0, 0), minutes=1)
    archiver.scan(env.conn, env.cfg, env.teslacam)
    env.conn.execute("UPDATE files SET uploaded_at=1")
    clip = next(folder.glob("*-front.mp4"))
    clip.write_bytes(b"\0" * 5000)
    stats = archiver.scan(env.conn, env.cfg, env.teslacam)
    assert stats["files_copied"] == 1
    row = env.conn.execute("SELECT size, uploaded_at FROM files WHERE name=?", (clip.name,)).fetchone()
    assert row["size"] == 5000 and row["uploaded_at"] is None


def test_deleted_event_is_not_reimported(env):
    make_event(env.teslacam, "SentryClips", datetime(2026, 10, 1, 14, 3, 22), minutes=1)
    archiver.scan(env.conn, env.cfg, env.teslacam)
    eid = "SentryClips/2026-10-01_14-03-22"
    env.conn.execute("UPDATE events SET state='deleted' WHERE id=?", (eid,))
    storage.purge_removed(env.conn, env.cfg)
    assert not (env.cfg.archive_dir / eid).exists()
    assert archiver.scan(env.conn, env.cfg, env.teslacam)["files_copied"] == 0
    assert not (env.cfg.archive_dir / eid).exists()


def test_car_cleanup_only_removes_archived(env):
    car = DirectoryCar(env.cfg.get("car.source_dir"))
    a = make_event(env.teslacam, "SentryClips", datetime(2026, 10, 1, 14, 0, 0), minutes=1)
    archiver.scan(env.conn, env.cfg, env.teslacam)
    b = make_event(env.teslacam, "SentryClips", datetime(2026, 10, 1, 15, 0, 0), minutes=1)  # not archived yet
    freed = archiver.car_cleanup(env.conn, env.cfg, car)
    assert freed["events"] == 1
    assert not a.exists() and b.exists()
    # archived copy is untouched
    assert (env.cfg.archive_dir / "SentryClips" / a.name / "event.json").exists()


def test_car_cleanup_waits_for_settle(env):
    env.cfg.data["car"]["settle_seconds"] = 3600
    car = DirectoryCar(env.cfg.get("car.source_dir"))
    a = make_event(env.teslacam, "SentryClips", datetime(2026, 10, 1, 14, 0, 0), minutes=1)
    archiver.scan(env.conn, env.cfg, env.teslacam)
    assert archiver.car_cleanup(env.conn, env.cfg, car)["events"] == 0
    assert a.exists()


def test_cycle_runs_end_to_end(env):
    make_event(env.teslacam, "SentryClips", datetime(2026, 10, 1, 14, 0, 0), minutes=2)
    archiver.cycle(env.conn, env.cfg, DirectoryCar(env.cfg.get("car.source_dir")))
    assert env.conn.execute("SELECT COUNT(*) FROM files").fetchone()[0] == 10


def _cycle_env(env, monkeypatch, free_pct):
    car = DirectoryCar(env.cfg.get("car.source_dir"))
    total = 1000
    monkeypatch.setattr(DirectoryCar, "usage", staticmethod(lambda root: (total, total * free_pct // 100)))
    monkeypatch.setattr(DirectoryCar, "idle_seconds", lambda self: 1e9)
    calls = []
    real = archiver.car_cleanup
    monkeypatch.setattr(archiver, "car_cleanup", lambda *a: calls.append(1) or real(*a))
    return car, calls


def test_cycle_skips_cleanup_when_nothing_to_free(env, monkeypatch):
    car, calls = _cycle_env(env, monkeypatch, free_pct=5)
    archiver.cycle(env.conn, env.cfg, car)  # empty drive: nothing archived, nothing to free
    assert calls == []


def test_cycle_cleanup_cooldown(env, monkeypatch):
    car, calls = _cycle_env(env, monkeypatch, free_pct=5)
    make_event(env.teslacam, "SentryClips", datetime(2026, 10, 1, 14, 0, 0), minutes=1)
    archiver.cycle(env.conn, env.cfg, car)  # archives, then cleans up (settle=0)
    assert len(calls) == 1
    make_event(env.teslacam, "SentryClips", datetime(2026, 10, 1, 15, 0, 0), minutes=1)
    archiver.cycle(env.conn, env.cfg, car)  # archives the new one, but cooldown blocks cleanup
    assert len(calls) == 1
    db.request(env.conn, "cleanup")
    archiver.cycle(env.conn, env.cfg, car)  # manual request bypasses the cooldown
    assert len(calls) == 2


def test_cycle_no_cleanup_with_free_space(env, monkeypatch):
    car, calls = _cycle_env(env, monkeypatch, free_pct=80)
    make_event(env.teslacam, "SentryClips", datetime(2026, 10, 1, 14, 0, 0), minutes=1)
    archiver.cycle(env.conn, env.cfg, car)
    assert calls == []
