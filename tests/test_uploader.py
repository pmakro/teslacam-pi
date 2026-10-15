from datetime import datetime

from teslacam import archiver, db, meter, uploader

from .conftest import make_event


def _archive(env):
    archiver.scan(env.conn, env.cfg, env.teslacam)


def test_queue_order(env):
    make_event(env.teslacam, "SentryClips", datetime(2026, 10, 2, 10, 0, 0), minutes=6)
    make_event(env.teslacam, "SentryClips", datetime(2026, 10, 1, 10, 0, 0), minutes=6)
    make_event(env.teslacam, "SavedClips", datetime(2026, 9, 30, 10, 0, 0), minutes=6)
    _archive(env)
    env.cfg.data["upload"]["max_batch_files"] = 100

    batch = uploader.next_batch(env.conn, env.cfg)
    # Saved trigger window first even though it is the oldest event
    assert {r["event_id"] for r in batch} == {"SavedClips/2026-09-30_10-00-00"}
    assert all(r["priority"] == 0 for r in batch)
    # 3 trigger-window minutes x 4 cams + event.json + thumb.png
    assert len(batch) == 3 * 4 + 2

    env.conn.execute("UPDATE files SET uploaded_at=1 WHERE event_id='SavedClips/2026-09-30_10-00-00' AND priority=0")
    batch = uploader.next_batch(env.conn, env.cfg)
    assert batch[0]["event_id"] == "SentryClips/2026-10-02_10-00-00" and batch[0]["priority"] == 0

    # user request jumps the queue
    env.conn.execute("UPDATE events SET upload_requested=1 WHERE id='SentryClips/2026-10-01_10-00-00'")
    assert uploader.next_batch(env.conn, env.cfg)[0]["event_id"] == "SentryClips/2026-10-01_10-00-00"


def test_unsettled_events_wait(env):
    env.cfg.data["car"]["settle_seconds"] = 3600
    make_event(env.teslacam, "SentryClips", datetime(2026, 10, 2, 10, 0, 0), minutes=1)
    _archive(env)
    assert uploader.next_batch(env.conn, env.cfg) == []


def test_fit_budget(env):
    make_event(env.teslacam, "SentryClips", datetime(2026, 10, 2, 10, 0, 0), minutes=1, size=1000)
    _archive(env)
    batch = uploader.next_batch(env.conn, env.cfg)
    assert len(uploader.fit_budget(batch, 2500, 1.0)) >= 2
    assert uploader.fit_budget(batch, 0, 1.0) == []


def test_upload_to_local_destination(env):
    make_event(env.teslacam, "SentryClips", datetime(2026, 10, 2, 10, 0, 0), minutes=4)
    _archive(env)
    up = uploader.Uploader(env.conn, env.cfg)
    for _ in range(10):
        up.step()
    pending = uploader.pending_summary(env.conn)
    assert pending["files"] == 0
    dest = env.nas / "SentryClips" / "2026-10-02_10-00-00"
    assert len(list(dest.glob("*.mp4"))) == 16 and (dest / "event.json").exists()
    assert meter.used_bytes(env.conn, env.cfg) == 16 * 1000 + sum(
        p.stat().st_size for p in dest.iterdir() if not p.name.endswith(".mp4"))
    assert db.status(env.conn, "uploader")["state"] == "idle"


def test_budget_blocks_upload(env):
    make_event(env.teslacam, "SentryClips", datetime(2026, 10, 2, 10, 0, 0), minutes=1)
    _archive(env)
    db.set_setting(env.conn, "budget.monthly_gb", 0)
    up = uploader.Uploader(env.conn, env.cfg)
    up.step()
    assert db.status(env.conn, "uploader")["state"] == "budget"
    assert uploader.pending_summary(env.conn)["files"] > 0


def test_paused(env):
    make_event(env.teslacam, "SentryClips", datetime(2026, 10, 2, 10, 0, 0), minutes=1)
    _archive(env)
    db.set_setting(env.conn, "upload.paused", "true")
    uploader.Uploader(env.conn, env.cfg).step()
    assert db.status(env.conn, "uploader")["state"] == "paused"


def test_failure_records_error(env):
    make_event(env.teslacam, "SentryClips", datetime(2026, 10, 2, 10, 0, 0), minutes=1)
    _archive(env)
    env.cfg.data["upload"]["remote_dir"] = str(env.tmp / "missing")
    up = uploader.Uploader(env.conn, env.cfg)
    up.step()
    assert db.status(env.conn, "uploader")["state"] == "offline"


def test_rsync_command_remote(env):
    env.cfg.data["upload"].update(host="nas", user="tc", ssh_key="/k", remote_dir="/volume1/TeslaCam")
    db.set_setting(env.conn, "upload.bwlimit_kbps", 500)
    rows = [{"event_id": "SentryClips/x", "name": "a.mp4"}]
    cmd = uploader.rsync_command(env.conn, env.cfg, rows)
    assert cmd[-1] == "tc@nas:/volume1/TeslaCam/"
    assert cmd[-2] == "SentryClips/x/a.mp4"
    assert "--bwlimit=500" in cmd and "-i /k" in cmd[cmd.index("-e") + 1]


def _failing_rsync(monkeypatch, code):
    monkeypatch.setattr(uploader, "rsync_command", lambda conn, cfg, batch: ["sh", "-c", f"echo boom >&2; exit {code}"])


def test_network_failures_do_not_use_up_attempts(env, monkeypatch):
    make_event(env.teslacam, "SentryClips", datetime(2026, 10, 2, 10, 0, 0), minutes=1)
    _archive(env)
    _failing_rsync(monkeypatch, 255)
    up = uploader.Uploader(env.conn, env.cfg)
    for _ in range(env.cfg.get("upload.max_attempts") + 2):
        up.reachable_until = 1e12  # skip the ssh pre-check
        up.step()
    assert env.conn.execute("SELECT MAX(attempts) FROM files").fetchone()[0] == 0
    assert db.status(env.conn, "uploader")["state"] == "offline"
    assert db.status(env.conn, "nas")["reachable"] is False
    assert uploader.next_batch(env.conn, env.cfg)  # still queued


def test_real_errors_count_toward_attempts(env, monkeypatch):
    make_event(env.teslacam, "SentryClips", datetime(2026, 10, 2, 10, 0, 0), minutes=1)
    _archive(env)
    _failing_rsync(monkeypatch, 23)  # partial transfer, e.g. permission denied on the NAS
    up = uploader.Uploader(env.conn, env.cfg)
    for _ in range(env.cfg.get("upload.max_attempts")):
        up.reachable_until = 1e12
        up.step()
    assert db.status(env.conn, "uploader")["state"] == "error"
    first = env.conn.execute("SELECT attempts, last_error FROM files ORDER BY priority, name LIMIT 1").fetchone()
    assert first["attempts"] == env.cfg.get("upload.max_attempts") and "boom" in first["last_error"]


def test_reachability_check_uses_rsync_listing(env, monkeypatch):
    env.cfg.data["upload"].update(host="100.64.0.5", user="teslacam", remote_dir="/volume1/TeslaCam")
    seen = {}

    def fake_run(cmd, **kwargs):
        seen["cmd"] = cmd
        import subprocess
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(uploader.subprocess, "run", fake_run)
    ok, _ = uploader.check_reachable(env.cfg)
    assert ok
    assert seen["cmd"][:2] == ["rsync", "--list-only"]
    assert seen["cmd"][-1] == "teslacam@100.64.0.5:/volume1/TeslaCam/"
    assert "true" not in seen["cmd"]
